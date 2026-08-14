/*
 * SPDX-License-Identifier: GPL-3.0-or-later
 * TurnSafe Session-1 default-off T0 sink.
 */

#include "TurnSafeDiagnostics.h"

#include "TurnSafeBuildProvenance.generated.h"
#include "UpdaterMSCKFPreview.h"
#include "feat/Feature.h"
#include "state/Propagator.h"
#include "state/State.h"
#include "utils/quat_ops.h"

#include <Eigen/Eigenvalues>

#include <algorithm>
#include <atomic>
#include <cerrno>
#include <cmath>
#include <cstring>
#include <fcntl.h>
#include <iomanip>
#include <limits>
#include <locale>
#include <map>
#include <new>
#include <set>
#include <sstream>
#include <stdexcept>
#include <sys/stat.h>
#include <tuple>
#include <unistd.h>

namespace ov_msckf {
namespace {

std::atomic<std::uint8_t> diagnostic_fault_stage{
    static_cast<std::uint8_t>(TurnSafeDiagnosticFaultStage::kNone)};
std::atomic<std::uint8_t> diagnostic_fault_kind{
    static_cast<std::uint8_t>(TurnSafeDiagnosticFaultKind::kNone)};

void inject_diagnostic_fault(TurnSafeDiagnosticFaultStage stage) {
  if (diagnostic_fault_stage.load(std::memory_order_acquire) !=
      static_cast<std::uint8_t>(stage)) {
    return;
  }
  switch (static_cast<TurnSafeDiagnosticFaultKind>(
      diagnostic_fault_kind.load(std::memory_order_acquire))) {
  case TurnSafeDiagnosticFaultKind::kNone: return;
  case TurnSafeDiagnosticFaultKind::kBadAlloc: throw std::bad_alloc();
  case TurnSafeDiagnosticFaultKind::kStdException:
    throw std::runtime_error("injected TurnSafe diagnostic failure");
  case TurnSafeDiagnosticFaultKind::kUnknown: throw 1;
  }
}

std::string json_escape(const std::string &value) {
  std::ostringstream output;
  for (const unsigned char byte : value) {
    switch (byte) {
    case '"': output << "\\\""; break;
    case '\\': output << "\\\\"; break;
    case '\b': output << "\\b"; break;
    case '\f': output << "\\f"; break;
    case '\n': output << "\\n"; break;
    case '\r': output << "\\r"; break;
    case '\t': output << "\\t"; break;
    default:
      if (byte < 0x20U) {
        output << "\\u" << std::hex << std::setw(4) << std::setfill('0')
               << static_cast<unsigned int>(byte) << std::dec;
      } else {
        output << static_cast<char>(byte);
      }
    }
  }
  return output.str();
}

std::string quote(const std::string &value) {
  return std::string("\"") + json_escape(value) + "\"";
}

std::string double_json(double value) {
  if (!std::isfinite(value)) {
    return "null";
  }
  std::ostringstream output;
  output.imbue(std::locale::classic());
  output << std::setprecision(std::numeric_limits<double>::max_digits10)
         << value;
  return output.str();
}

std::string timestamp_key(double value) {
  std::uint64_t bits = 0U;
  static_assert(sizeof(bits) == sizeof(value), "binary64 timestamp required");
  std::memcpy(&bits, &value, sizeof(bits));
  std::ostringstream output;
  output << "f64:0x" << std::hex << std::setw(16) << std::setfill('0')
         << bits;
  return output.str();
}

bool matrix_value_valid(const TurnSafeMatrixValue &matrix) {
  if (matrix.rows == 0U || matrix.cols == 0U ||
      matrix.rows > std::numeric_limits<std::size_t>::max() / matrix.cols ||
      matrix.values.size() != matrix.rows * matrix.cols) {
    return false;
  }
  return std::all_of(matrix.values.begin(), matrix.values.end(),
                     [](double value) { return std::isfinite(value); });
}

bool camera_provenance_valid(const TurnSafePriorCameraValue &camera) {
  if (camera.width <= 0 || camera.height <= 0 || camera.model != "RADTAN" ||
      !matrix_value_valid(camera.intrinsic_value) ||
      camera.intrinsic_value.rows != 8U ||
      camera.intrinsic_value.cols != 1U ||
      !matrix_value_valid(camera.extrinsic_value) ||
      camera.extrinsic_value.rows != 7U ||
      camera.extrinsic_value.cols != 1U) {
    return false;
  }
  double quaternion_squared_norm = 0.0;
  for (std::size_t index = 0U; index < 4U; ++index)
    quaternion_squared_norm += camera.extrinsic_value.values[index] *
                               camera.extrinsic_value.values[index];
  return std::isfinite(quaternion_squared_norm) &&
         std::fabs(quaternion_squared_norm - 1.0) <= 1e-10;
}

std::string matrix_json(const TurnSafeMatrixValue &matrix) {
  if (!matrix_value_valid(matrix)) {
    return "{\"status\":\"NONFINITE\",\"reason\":\"MATRIX_VALUE_INVALID\"}";
  }
  std::ostringstream output;
  output << "{\"status\":\"AVAILABLE\",\"rows\":" << matrix.rows
         << ",\"cols\":" << matrix.cols << ",\"values\":[";
  for (std::size_t index = 0U; index < matrix.values.size(); ++index) {
    if (index != 0U) output << ',';
    output << double_json(matrix.values[index]);
  }
  output << "]}";
  return output.str();
}

TurnSafeMatrixValue capture_matrix(const Eigen::MatrixXd &matrix) {
  TurnSafeMatrixValue output;
  if (matrix.rows() <= 0 || matrix.cols() <= 0 || !matrix.allFinite()) {
    return output;
  }
  output.rows = static_cast<std::size_t>(matrix.rows());
  output.cols = static_cast<std::size_t>(matrix.cols());
  output.values.reserve(output.rows * output.cols);
  for (Eigen::Index row = 0; row < matrix.rows(); ++row) {
    for (Eigen::Index col = 0; col < matrix.cols(); ++col) {
      output.values.push_back(matrix(row, col));
    }
  }
  return output;
}

void fnv_mix(std::uint64_t &hash, const void *data, std::size_t count) {
  const auto *bytes = static_cast<const unsigned char *>(data);
  for (std::size_t index = 0U; index < count; ++index) {
    hash ^= static_cast<std::uint64_t>(bytes[index]);
    hash *= UINT64_C(1099511628211);
  }
}

std::string prior_fingerprint(const MSCKFUpdatePriorSnapshot &prior) {
  std::uint64_t hash = UINT64_C(1469598103934665603);
  if (prior.filter.covariance.size() > 0) {
    fnv_mix(hash, prior.filter.covariance.data(),
            static_cast<std::size_t>(prior.filter.covariance.size()) *
                sizeof(double));
  }
  fnv_mix(hash, &prior.timestamp, sizeof(prior.timestamp));
  for (const auto &binding : prior.clone_bindings) {
    fnv_mix(hash, &binding.timestamp, sizeof(binding.timestamp));
    fnv_mix(hash, &binding.covariance_id, sizeof(binding.covariance_id));
  }
  std::ostringstream output;
  output << "fnv1a64:" << std::hex << std::setw(16) << std::setfill('0')
         << hash;
  return output.str();
}

bool same_timestamp(double left, double right) noexcept {
  std::uint64_t left_bits = 0U;
  std::uint64_t right_bits = 0U;
  std::memcpy(&left_bits, &left, sizeof(left_bits));
  std::memcpy(&right_bits, &right, sizeof(right_bits));
  return left_bits == right_bits;
}

bool finite_vector3(const std::array<double, 3U> &value) noexcept {
  return std::isfinite(value[0]) && std::isfinite(value[1]) &&
         std::isfinite(value[2]);
}

std::array<double, 3U> interpolate_omega(
    const TurnSafeImuSampleValue &lower,
    const TurnSafeImuSampleValue &upper, double timestamp) {
  const double span = upper.timestamp - lower.timestamp;
  if (!(span > 0.0) || !std::isfinite(span))
    throw std::runtime_error("invalid IMU interpolation bracket");
  const double alpha = (timestamp - lower.timestamp) / span;
  std::array<double, 3U> output{{0.0, 0.0, 0.0}};
  for (std::size_t axis = 0U; axis < 3U; ++axis) {
    output[axis] = lower.omega_rad_s[axis] +
                   alpha * (upper.omega_rad_s[axis] -
                            lower.omega_rad_s[axis]);
  }
  if (!finite_vector3(output))
    throw std::runtime_error("nonfinite interpolated gyro");
  return output;
}

Eigen::Vector3d eigen_vector3(const std::array<double, 3U> &value) {
  return Eigen::Vector3d(value[0], value[1], value[2]);
}

TurnSafeGyroSummary summarize_gyro_series(
    const std::vector<double> &timestamps,
    const std::vector<std::array<double, 3U>> &omega) {
  TurnSafeGyroSummary output;
  if (timestamps.size() < 2U || timestamps.size() != omega.size()) {
    output.reason = "INSUFFICIENT_INTERVAL_SUPPORT";
    return output;
  }
  Eigen::Vector3d vector_integral = Eigen::Vector3d::Zero();
  double norm_integral = 0.0;
  double squared_norm_integral = 0.0;
  double maximum_norm = 0.0;
  Eigen::Matrix3d delta_rotation = Eigen::Matrix3d::Identity();
  for (const auto &sample : omega) {
    if (!finite_vector3(sample)) {
      output.reason = "GYRO_SERIES_NONFINITE";
      return output;
    }
    maximum_norm = std::max(maximum_norm, eigen_vector3(sample).norm());
  }
  for (std::size_t index = 0U; index + 1U < timestamps.size(); ++index) {
    const double dt = timestamps[index + 1U] - timestamps[index];
    if (!(dt > 0.0) || !std::isfinite(dt)) {
      output.reason = "KNOT_TIMESTAMPS_NOT_STRICTLY_INCREASING";
      return output;
    }
    const Eigen::Vector3d left = eigen_vector3(omega[index]);
    const Eigen::Vector3d right = eigen_vector3(omega[index + 1U]);
    const Eigen::Vector3d average = 0.5 * (left + right);
    vector_integral.noalias() += average * dt;
    norm_integral += 0.5 * (left.norm() + right.norm()) * dt;
    squared_norm_integral +=
        0.5 * (left.squaredNorm() + right.squaredNorm()) * dt;
    inject_diagnostic_fault(TurnSafeDiagnosticFaultStage::kSo3Composition);
    delta_rotation = ov_core::exp_so3(-average * dt) * delta_rotation;
  }
  const double duration = timestamps.back() - timestamps.front();
  if (!(duration > 0.0) || !std::isfinite(duration) ||
      !vector_integral.allFinite() || !std::isfinite(norm_integral) ||
      !std::isfinite(squared_norm_integral) ||
      !delta_rotation.allFinite()) {
    output.reason = "INTEGRATION_NONFINITE";
    return output;
  }
  const Eigen::Vector3d mean = vector_integral / duration;
  Eigen::Vector4d quaternion = ov_core::rot_2_quat(delta_rotation);
  if (quaternion(3) < 0.0 ||
      (quaternion(3) == 0.0 &&
       (quaternion(0) < 0.0 ||
        (quaternion(0) == 0.0 &&
         (quaternion(1) < 0.0 ||
          (quaternion(1) == 0.0 && quaternion(2) < 0.0)))))) {
    quaternion = -quaternion;
  }
  const double vector_norm = quaternion.head<3>().norm();
  const double angle = 2.0 * std::atan2(vector_norm,
                                        std::fabs(quaternion(3)));
  output.max_norm_rad_s = maximum_norm;
  output.rms_norm_rad_s = std::sqrt(squared_norm_integral / duration);
  output.mean_omega_rad_s = {{mean(0), mean(1), mean(2)}};
  output.integral_norm_rad = norm_integral;
  output.integral_omega_rad = {{vector_integral(0), vector_integral(1),
                                vector_integral(2)}};
  for (Eigen::Index row = 0; row < 3; ++row) {
    for (Eigen::Index col = 0; col < 3; ++col) {
      output.delta_rotation_matrix_row_major[
          static_cast<std::size_t>(row * 3 + col)] =
          delta_rotation(row, col);
    }
  }
  output.delta_rotation_jpl_xyzw = {{quaternion(0), quaternion(1),
                                      quaternion(2), quaternion(3)}};
  output.delta_rotation_angle_rad = angle;
  if (vector_norm > 0.0 && std::isfinite(vector_norm)) {
    const Eigen::Vector3d axis = -quaternion.head<3>() / vector_norm;
    output.delta_rotation_axis = {{axis(0), axis(1), axis(2)}};
    output.delta_rotation_axis_available = axis.allFinite();
    output.delta_rotation_axis_reason =
        output.delta_rotation_axis_available ? "NONE" : "ROTATION_AXIS_NONFINITE";
  } else {
    output.delta_rotation_axis_reason = "ROTATION_AXIS_UNDEFINED";
  }
  output.available = std::isfinite(output.max_norm_rad_s) &&
                     std::isfinite(output.rms_norm_rad_s) &&
                     finite_vector3(output.mean_omega_rad_s) &&
                     std::isfinite(output.integral_norm_rad) &&
                     finite_vector3(output.integral_omega_rad) &&
                     std::isfinite(output.delta_rotation_angle_rad);
  output.reason = output.available ? "NONE" : "INTEGRATION_NONFINITE";
  return output;
}

std::string event_number_json(bool available, double value,
                              const char *reason) {
  if (available && std::isfinite(value)) {
    return std::string("{\"status\":\"AVAILABLE\",\"value\":") +
           double_json(value) + ",\"reason\":\"NONE\"}";
  }
  const bool explicitly_nonfinite =
      reason != nullptr && std::strstr(reason, "NONFINITE") != nullptr;
  return std::string("{\"status\":") +
         quote((available || explicitly_nonfinite) ? "NONFINITE"
                                                   : "NOT_AVAILABLE") +
         ",\"reason\":" +
         quote(available ? "VALUE_NONFINITE" : reason) + "}";
}

std::string event_uint_json(bool available, std::uint64_t value,
                            const char *reason) {
  if (available) {
    return std::string("{\"status\":\"AVAILABLE\",\"value\":") +
           std::to_string(value) + ",\"reason\":\"NONE\"}";
  }
  return std::string("{\"status\":\"NOT_AVAILABLE\",\"reason\":") +
         quote(reason) + "}";
}

std::string event_bool_json(bool available, bool value,
                            const char *reason) {
  if (available) {
    return std::string("{\"status\":\"AVAILABLE\",\"value\":") +
           (value ? "true" : "false") + ",\"reason\":\"NONE\"}";
  }
  return std::string("{\"status\":\"NOT_AVAILABLE\",\"reason\":") +
         quote(reason) + "}";
}

void write_vector3(std::ostringstream &output,
                   const std::array<double, 3U> &value) {
  output << '[' << double_json(value[0]) << ',' << double_json(value[1])
         << ',' << double_json(value[2]) << ']';
}

void write_endpoint(std::ostringstream &output,
                    const TurnSafeImuEndpointEvidence &endpoint,
                    const std::string &interval_reason) {
  output << "{\"status\":" << quote(endpoint.status)
         << ",\"reason\":"
         << quote(endpoint.reason == "NOT_CAPTURED" ? interval_reason
                                                     : endpoint.reason)
         << ",\"lower_timestamp\":"
         << event_number_json(endpoint.lower_timestamp_available,
                              endpoint.lower_timestamp,
                              "LOWER_BRACKET_UNAVAILABLE")
         << ",\"upper_timestamp\":"
         << event_number_json(endpoint.upper_timestamp_available,
                              endpoint.upper_timestamp,
                              "UPPER_BRACKET_UNAVAILABLE") << '}';
}

void write_gyro_summary(std::ostringstream &output,
                        const TurnSafeGyroSummary &summary,
                        const std::string &interval_reason) {
  if (!summary.available) {
    output << "{\"status\":\"NOT_AVAILABLE\",\"reason\":"
           << quote(summary.reason == "NOT_CAPTURED" ? interval_reason
                                                      : summary.reason) << '}';
    return;
  }
  output << "{\"status\":\"AVAILABLE\",\"reason\":\"NONE\""
         << ",\"max_norm_rad_s\":" << double_json(summary.max_norm_rad_s)
         << ",\"rms_norm_rad_s\":" << double_json(summary.rms_norm_rad_s)
         << ",\"mean_omega_xyz_rad_s\":";
  write_vector3(output, summary.mean_omega_rad_s);
  output << ",\"integral_norm_rad\":"
         << double_json(summary.integral_norm_rad)
         << ",\"integral_omega_xyz_rad\":";
  write_vector3(output, summary.integral_omega_rad);
  output << ",\"delta_rotation_matrix_row_major\":[";
  for (std::size_t index = 0U;
       index < summary.delta_rotation_matrix_row_major.size(); ++index) {
    if (index != 0U) output << ',';
    output << double_json(summary.delta_rotation_matrix_row_major[index]);
  }
  output << "],\"delta_rotation_jpl_xyzw\":[";
  for (std::size_t index = 0U;
       index < summary.delta_rotation_jpl_xyzw.size(); ++index) {
    if (index != 0U) output << ',';
    output << double_json(summary.delta_rotation_jpl_xyzw[index]);
  }
  output << "],\"delta_rotation_angle_rad\":"
         << double_json(summary.delta_rotation_angle_rad)
         << ",\"delta_rotation_axis\":";
  if (summary.delta_rotation_axis_available) {
    output << "{\"status\":\"AVAILABLE\",\"reason\":\"NONE\",\"value\":";
    write_vector3(output, summary.delta_rotation_axis);
    output << '}';
  } else {
    output << "{\"status\":\"NOT_AVAILABLE\",\"reason\":"
           << quote(summary.delta_rotation_axis_reason) << '}';
  }
  output << '}';
}

void write_causal_interval(std::ostringstream &output,
                           const TurnSafeCausalImuInterval &interval,
                           std::uint64_t callback_id) {
  output << "{\"gyro_interval_id\":" << interval.gyro_interval_id
         << ",\"callback_id\":" << callback_id
         << ",\"status\":"
         << quote(interval.available ? "AVAILABLE" : "NOT_AVAILABLE")
         << ",\"reason\":" << quote(interval.reason)
         << ",\"units\":{\"time\":\"s\",\"angular_rate\":\"rad/s\",\"rotation\":\"rad\"}"
         << ",\"frame_convention\":{\"raw\":\"native_gyroscope_measurement_frame\",\"bias_corrected\":\"native_gyroscope_measurement_frame_wm_minus_bias_g\",\"delta\":\"native_gyroscope_frame_passive_left_composed_Exp_minus_omega_dt_diagnostic\"}"
         << ",\"camera_frame_ids\":[";
  for (std::size_t index = 0U; index < interval.camera_frame_ids.size(); ++index) {
    if (index != 0U) output << ',';
    output << interval.camera_frame_ids[index];
  }
  output << "],\"current_callback_camera_timestamp\":"
         << event_number_json(std::isfinite(interval.current_callback_timestamp),
                              interval.current_callback_timestamp,
                              "CALLBACK_TIMESTAMP_NONFINITE")
         << ",\"previous_processed_callback_camera_timestamp\":"
         << event_number_json(interval.previous_callback_timestamp_available,
                              interval.previous_callback_timestamp,
                              "FIRST_CALLBACK")
         << ",\"interval_start_camera_s\":"
         << event_number_json(interval.previous_callback_timestamp_available &&
                                  std::isfinite(interval.previous_callback_timestamp),
                              interval.previous_callback_timestamp,
                              interval.reason.c_str())
         << ",\"interval_end_camera_s\":"
         << event_number_json(std::isfinite(interval.current_callback_timestamp),
                              interval.current_callback_timestamp,
                              interval.reason.c_str())
         << ",\"duration_s\":"
         << event_number_json(interval.duration_available, interval.duration_s,
                              interval.reason.c_str())
         << ",\"dt_CAMtoIMU_s\":"
         << event_number_json(interval.imu_time_offset_available,
                              interval.imu_time_offset_s,
                              interval.imu_time_offset_reason.c_str())
         << ",\"clock_equation\":\"t_imu=t_camera+dt_CAMtoIMU\""
         << ",\"interval_start_imu_s\":"
         << event_number_json(interval.imu_interval_endpoints_available,
                              interval.imu_interval_start_s,
                              interval.reason.c_str())
         << ",\"interval_end_imu_s\":"
         << event_number_json(interval.imu_interval_endpoints_available,
                              interval.imu_interval_end_s,
                              interval.reason.c_str())
         << ",\"support\":{\"first_timestamp_s\":"
         << event_number_json(interval.first_support_timestamp_available,
                              interval.first_support_timestamp,
                              interval.reason.c_str())
         << ",\"last_timestamp_s\":"
         << event_number_json(interval.last_support_timestamp_available,
                              interval.last_support_timestamp,
                              interval.reason.c_str())
         << ",\"source_sample_count\":"
         << event_uint_json(interval.source_sample_count_available,
                            interval.source_sample_count,
                            interval.source_sample_count_reason.c_str())
         << ",\"start_endpoint\":";
  write_endpoint(output, interval.start_endpoint, interval.reason);
  output << ",\"end_endpoint\":";
  write_endpoint(output, interval.end_endpoint, interval.reason);
  output << ",\"maximum_internal_sample_gap_s\":"
         << event_number_json(interval.maximum_internal_gap_available,
                              interval.maximum_internal_gap_s,
                              "INSUFFICIENT_SUPPORT_FOR_GAP")
         << ",\"coverage_fraction\":"
         << event_number_json(interval.coverage_fraction_available,
                              interval.coverage_fraction,
                              "INTERVAL_ENDPOINTS_UNAVAILABLE")
         << ",\"endpoint_policy\":\"exact_or_linear_bracket_no_extrapolation.v1\"}"
         << ",\"gyro_bias_snapshot_xyz_rad_s\":";
  if (interval.gyro_bias_available) {
    output << "{\"status\":\"AVAILABLE\",\"reason\":\"NONE\",\"value\":";
    write_vector3(output, interval.gyro_bias_rad_s);
    output << '}';
  } else {
    output << "{\"status\":\"NOT_AVAILABLE\",\"reason\":"
           << quote(interval.gyro_bias_reason) << '}';
  }
  output << ",\"knots\":{\"status\":"
         << quote(interval.knot_timestamps_s.empty() ? "NOT_AVAILABLE" : "AVAILABLE")
         << ",\"reason\":"
         << quote(interval.knot_timestamps_s.empty() ? interval.reason : "NONE");
  if (!interval.knot_timestamps_s.empty()) {
    output << ",\"timestamp_value_s\":[";
    for (std::size_t index = 0U; index < interval.knot_timestamps_s.size(); ++index) {
      if (index != 0U) output << ',';
      output << double_json(interval.knot_timestamps_s[index]);
    }
    output << "],\"timestamp_key\":[";
    for (std::size_t index = 0U; index < interval.knot_timestamps_s.size(); ++index) {
      if (index != 0U) output << ',';
      output << quote(timestamp_key(interval.knot_timestamps_s[index]));
    }
    output << "],\"raw_omega_xyz_rad_s\":[";
    for (std::size_t index = 0U; index < interval.raw_omega_rad_s.size(); ++index) {
      if (index != 0U) output << ',';
      write_vector3(output, interval.raw_omega_rad_s[index]);
    }
    output << "],\"bias_corrected_omega_xyz_rad_s\":";
    if (interval.bias_corrected_omega_rad_s.size() ==
        interval.knot_timestamps_s.size()) {
      output << '[';
      for (std::size_t index = 0U;
           index < interval.bias_corrected_omega_rad_s.size(); ++index) {
        if (index != 0U) output << ',';
        write_vector3(output, interval.bias_corrected_omega_rad_s[index]);
      }
      output << ']';
    } else {
      output << "{\"status\":\"NOT_AVAILABLE\",\"reason\":"
             << quote(interval.gyro_bias_reason) << '}';
    }
  }
  output << "},\"integration_method\":\"piecewise_linear_trapezoid.v1\""
         << ",\"raw_summary\":";
  write_gyro_summary(output, interval.raw_summary, interval.reason);
  output << ",\"bias_corrected_summary\":";
  write_gyro_summary(output, interval.bias_corrected_summary, interval.reason);
  output << '}';
}

void write_expected_row_key(std::ostringstream &output,
                            const char *stream_id, bool available,
                            double timestamp, const std::string &reason) {
  if (!available || !std::isfinite(timestamp)) {
    output << "{\"status\":\"NOT_AVAILABLE\",\"reason\":"
           << quote(reason) << '}';
    return;
  }
  output << "{\"status\":\"AVAILABLE\",\"reason\":\"NONE\",\"stream_id\":"
         << quote(stream_id)
         << ",\"timestamp_value\":" << double_json(timestamp)
         << ",\"timestamp_key\":" << quote(timestamp_key(timestamp))
         << ",\"row_ordinal\":{\"status\":\"NOT_AVAILABLE\",\"reason\":\"ROW_ORDINAL_OFFLINE_ONLY\"}}";
}

void write_outcome_association_keys(
    std::ostringstream &output,
    const TurnSafeDiagnosticsOptions &options,
    std::uint64_t callback_index, double callback_timestamp,
    const TurnSafeOutcomeAssociationKeys &keys,
    bool update_available, const TurnSafeUpdateRecord &update,
    bool duration_available, double duration) {
  std::uint64_t accepted_full_factor_count = 0U;
  if (update_available) {
    for (const auto &attempt : update.attempts) {
      if (attempt.accepted_full_factor) ++accepted_full_factor_count;
    }
  }
  const bool full_accepted = update_available &&
      update.baseline_commit_occurred &&
      !update.baseline_accepted_ids.empty();
  output << "{\"callback_id\":" << callback_index
         << ",\"callback_camera_timestamp_value\":"
         << double_json(callback_timestamp)
         << ",\"callback_camera_timestamp_key\":"
         << quote(timestamp_key(callback_timestamp))
         << ",\"estimator_initialized_before\":"
         << event_bool_json(keys.initialized_before_available,
                            keys.initialized_before,
                            "INITIALIZED_STATUS_NOT_EXPOSED")
         << ",\"estimator_initialized_after\":"
         << event_bool_json(keys.initialized_after_available,
                            keys.initialized_after,
                            "INITIALIZED_STATUS_NOT_EXPOSED")
         << ",\"estimator_valid_before\":"
         << event_bool_json(keys.estimator_valid_before_available,
                            keys.estimator_valid_before,
                            "STATE_VALIDITY_NOT_EXPOSED")
         << ",\"estimator_valid_after\":"
         << event_bool_json(keys.estimator_valid_after_available,
                            keys.estimator_valid_after,
                            "STATE_VALIDITY_NOT_EXPOSED")
         << ",\"state_timestamp_before\":"
         << event_number_json(keys.state_timestamp_before_available,
                              keys.state_timestamp_before,
                              keys.state_timestamp_before_reason.c_str())
         << ",\"state_timestamp_after\":"
         << event_number_json(keys.state_timestamp_after_available,
                              keys.state_timestamp_after,
                              keys.state_timestamp_after_reason.c_str())
         << ",\"expected_state_row_key\":";
  write_expected_row_key(output, "state_estimate",
                         keys.expected_output_timestamp_available,
                         keys.expected_output_timestamp,
                         keys.expected_output_timestamp_reason);
  output << ",\"expected_deviation_row_key\":";
  write_expected_row_key(output, "state_deviation",
                         keys.expected_output_timestamp_available,
                         keys.expected_output_timestamp,
                         keys.expected_output_timestamp_reason);
  output << ",\"expected_pose_row_key\":";
  write_expected_row_key(output, "trajectory_tum",
                         keys.expected_output_timestamp_available,
                         keys.expected_output_timestamp,
                         keys.expected_output_timestamp_reason);
  output << ",\"pose_stream_write_status\":{\"status\":\"NOT_EXPOSED\",\"reason\":\"POSE_WRITE_STATUS_OFFLINE_ONLY\"}"
         << ",\"reset_status\":{\"status\":\"NOT_EXPOSED\",\"reason\":\"RESET_STATUS_NOT_EXPOSED_BY_NATIVE_PATH\"}"
         << ",\"nonfinite_observed\":";
  if (keys.nonfinite_observed_available) {
    output << "{\"status\":\"AVAILABLE\",\"value\":"
           << (keys.nonfinite_observed ? "true" : "false")
           << ",\"observation_scope\":\"nominal_imu_and_state_timestamp\",\"reason\":\"NONE\"}";
  } else {
    output << "{\"status\":\"NOT_AVAILABLE\",\"reason\":"
           << quote(keys.nonfinite_observed_reason) << '}';
  }
  output
         << ",\"callback_incomplete\":{\"status\":\"AVAILABLE\",\"value\":"
         << (keys.callback_incomplete ? "true" : "false")
         << ",\"reason\":\"NONE\",\"completion_reason\":"
         << quote(keys.callback_completion_reason) << '}'
         << ",\"run_completeness\":{\"status\":\"NOT_AVAILABLE\",\"reason\":\"RUN_COMPLETENESS_POST_CLOSE_ONLY\"}"
         << ",\"ordinary_accepted_full_factor_count\":"
         << event_uint_json(update_available, accepted_full_factor_count,
                            "UPDATER_NOT_REACHED")
         << ",\"ordinary_full_visual_update_accepted\":"
         << event_bool_json(update_available, full_accepted,
                            "UPDATER_NOT_REACHED")
         << ",\"time_since_last_accepted_ordinary_full_update_camera_s\":";
  if (duration_available) {
    output << event_number_json(true,
                                duration,
                                "NONE");
  } else {
    output << "{\"status\":\"NOT_AVAILABLE\",\"reason\":\"SEE_BASELINE_DECISION_TYPED_REASON\"}";
  }
  output << ",\"reference_association\":{\"status\":\"NOT_AVAILABLE\",\"reason\":\"REFERENCE_ASSOCIATION_OFFLINE_ONLY\"}"
         << ",\"identity\":{\"run_identity\":";
  if (options.run_identity.empty())
    output << "{\"status\":\"NOT_AVAILABLE\",\"reason\":\"OPAQUE_RUN_IDENTITY_NOT_CONFIGURED\"}";
  else
    output << quote(options.run_identity);
  output << ",\"source_sha\":" << quote(options.source_sha)
         << ",\"source_tree\":" << quote(options.source_tree)
         << ",\"source_snapshot_sha256\":"
         << quote(options.source_snapshot_sha256)
         << ",\"build_provenance_id\":"
         << quote(options.build_provenance_id)
         << ",\"config_sha256\":" << quote(options.config_sha256)
         << ",\"calibration_sha256\":"
         << quote(options.calibration_sha256) << "}}";
}

void fnv_mix_u64_be(std::uint64_t &hash, std::uint64_t value) {
  for (int shift = 56; shift >= 0; shift -= 8) {
    const unsigned char byte =
        static_cast<unsigned char>((value >> shift) & UINT64_C(0xff));
    fnv_mix(hash, &byte, 1U);
  }
}

std::string canonical_matrix_hash(const TurnSafeMatrixValue &matrix) {
  if (!matrix_value_valid(matrix)) return "UNAVAILABLE";
  std::uint64_t hash = UINT64_C(1469598103934665603);
  fnv_mix_u64_be(hash, static_cast<std::uint64_t>(matrix.rows));
  fnv_mix_u64_be(hash, static_cast<std::uint64_t>(matrix.cols));
  for (double value : matrix.values) {
    std::uint64_t bits = 0U;
    std::memcpy(&bits, &value, sizeof(bits));
    fnv_mix_u64_be(hash, bits);
  }
  std::ostringstream output;
  output << "fnv1a64:" << std::hex << std::setw(16) << std::setfill('0')
         << hash;
  return output.str();
}

bool pose_rotation(const TurnSafeMatrixValue &pose,
                   Eigen::Matrix3d &rotation) {
  if (pose.rows != 7U || pose.cols != 1U || pose.values.size() != 7U ||
      !std::all_of(pose.values.begin(), pose.values.end(),
                   [](double value) { return std::isfinite(value); })) {
    return false;
  }
  Eigen::Vector4d quaternion(pose.values[0], pose.values[1],
                             pose.values[2], pose.values[3]);
  const double norm = quaternion.norm();
  if (!std::isfinite(norm) || std::fabs(norm - 1.0) > 1e-10) return false;
  quaternion /= norm;
  rotation = ov_core::quat_2_Rot(quaternion);
  return rotation.allFinite();
}

void write_rotation(std::ostringstream &output,
                    const TurnSafeMatrixValue *pose,
                    const char *reason) {
  Eigen::Matrix3d rotation;
  if (pose == nullptr || !pose_rotation(*pose, rotation)) {
    output << "{\"status\":\"NOT_AVAILABLE\",\"reason\":"
           << quote(reason) << '}';
    return;
  }
  output << "{\"status\":\"AVAILABLE\",\"reason\":\"NONE\",\"rows\":3,\"cols\":3,\"values_row_major\":[";
  for (Eigen::Index row = 0; row < 3; ++row) {
    for (Eigen::Index col = 0; col < 3; ++col) {
      if (row != 0 || col != 0) output << ',';
      output << double_json(rotation(row, col));
    }
  }
  output << "]}";
}

void write_pixel_value(std::ostringstream &output, bool available,
                       const std::array<double, 2U> &pixel,
                       const char *missing_reason,
                       const char *nonfinite_reason) {
  if (!available) {
    output << "{\"status\":\"NOT_AVAILABLE\",\"reason\":"
           << quote(missing_reason) << '}';
    return;
  }
  if (!std::isfinite(pixel[0]) || !std::isfinite(pixel[1])) {
    output << "{\"status\":\"NONFINITE\",\"reason\":"
           << quote(nonfinite_reason) << '}';
    return;
  }
  output << "{\"status\":\"AVAILABLE\",\"reason\":\"NONE\",\"value\":["
         << double_json(pixel[0]) << ',' << double_json(pixel[1]) << "]}";
}

void write_unit_bearing(std::ostringstream &output,
                        const TurnSafeObservationValue &observation,
                        const char *missing_reason,
                        const char *nonfinite_reason) {
  if (!observation.uv_normalized_available) {
    output << "{\"status\":\"NOT_AVAILABLE\",\"reason\":"
           << quote(missing_reason) << '}';
    return;
  }
  const Eigen::Vector3d ray(observation.uv_normalized[0],
                            observation.uv_normalized[1], 1.0);
  const double norm = ray.norm();
  if (!ray.allFinite() || !(norm > 0.0) || !std::isfinite(norm)) {
    output << "{\"status\":\"NONFINITE\",\"reason\":"
           << quote(nonfinite_reason) << '}';
    return;
  }
  const Eigen::Vector3d bearing = ray / norm;
  output << "{\"status\":\"AVAILABLE\",\"reason\":\"NONE\",\"frame\":\"camera\",\"value\":["
         << double_json(bearing(0)) << ',' << double_json(bearing(1))
         << ',' << double_json(bearing(2)) << "]}";
}

void write_image_cell(std::ostringstream &output,
                      const TurnSafeObservationValue &observation,
                      const TurnSafePriorCameraValue *camera) {
  if (camera == nullptr || camera->width <= 0 || camera->height <= 0) {
    output << "{\"status\":\"NOT_AVAILABLE\",\"reason\":\"IMAGE_DIMENSIONS_UNAVAILABLE\"}";
    return;
  }
  if (!observation.uv_available) {
    output << "{\"status\":\"NOT_AVAILABLE\",\"reason\":\"RAW_PIXEL_UNAVAILABLE\"}";
    return;
  }
  if (!std::isfinite(observation.uv[0]) ||
      !std::isfinite(observation.uv[1])) {
    output << "{\"status\":\"NONFINITE\",\"reason\":\"RAW_PIXEL_NONFINITE\"}";
    return;
  }
  output << "{\"status\":\"AVAILABLE\",\"reason\":\"NONE\",\"representation\":\"continuous_pixel_center_fraction.v1\",\"value\":["
         << double_json((observation.uv[0] + 0.5) /
                        static_cast<double>(camera->width))
         << ','
         << double_json((observation.uv[1] + 0.5) /
                        static_cast<double>(camera->height))
         << "]}";
}

const MSCKFUpdatePriorCloneBinding *find_binding(
    const MSCKFUpdatePriorSnapshot &prior, double timestamp) {
  for (const auto &binding : prior.clone_bindings) {
    if (same_timestamp(binding.timestamp, timestamp)) return &binding;
  }
  return nullptr;
}

const MSCKFUpdatePriorNominalBlock *find_nominal(
    const MSCKFUpdatePriorSnapshot &prior, Eigen::Index covariance_id) {
  for (const auto &block : prior.nominal_blocks) {
    if (block.covariance_id == covariance_id) return &block;
  }
  return nullptr;
}

bool validate_prior_partition(const MSCKFUpdatePriorSnapshot &prior) {
  const Eigen::MatrixXd &covariance = prior.filter.covariance;
  if (covariance.rows() <= 0 || covariance.cols() != covariance.rows() ||
      !covariance.allFinite()) {
    return false;
  }
  Eigen::Index expected = 0;
  for (const auto &block : prior.filter.state_blocks) {
    if (block.covariance_id != expected || block.offset != expected ||
        block.size <= 0 || block.size > covariance.rows() - expected) {
      return false;
    }
    expected += block.size;
  }
  return expected == covariance.rows();
}

struct CandidateKey {
  std::size_t camera_id = 0U;
  double source = 0.0;
  double target = 0.0;
  std::uint64_t feature_id = 0U;
  std::uint64_t detached_index = 0U;
  std::size_t source_observation_index = 0U;
  std::size_t target_observation_index = 0U;
};

bool candidate_key_less(const CandidateKey &left,
                        const CandidateKey &right) {
  if (left.camera_id != right.camera_id)
    return left.camera_id < right.camera_id;
  const std::string left_source = timestamp_key(left.source);
  const std::string right_source = timestamp_key(right.source);
  if (left_source != right_source) return left_source < right_source;
  const std::string left_target = timestamp_key(left.target);
  const std::string right_target = timestamp_key(right.target);
  if (left_target != right_target) return left_target < right_target;
  if (left.feature_id != right.feature_id)
    return left.feature_id < right.feature_id;
  if (left.detached_index != right.detached_index)
    return left.detached_index < right.detached_index;
  if (left.source_observation_index != right.source_observation_index)
    return left.source_observation_index < right.source_observation_index;
  return left.target_observation_index < right.target_observation_index;
}

std::vector<CandidateKey> enumerate_candidates(
    const TurnSafeFullTrackAttempt &attempt) {
  std::vector<CandidateKey> output;
  for (std::size_t left = 0U; left < attempt.ordered_observations.size();
       ++left) {
    const auto &first = attempt.ordered_observations[left];
    for (std::size_t right = left + 1U;
         right < attempt.ordered_observations.size(); ++right) {
      const auto &second = attempt.ordered_observations[right];
      if (first.camera_id != second.camera_id ||
          !std::isfinite(first.timestamp) ||
          !std::isfinite(second.timestamp) ||
          same_timestamp(first.timestamp, second.timestamp)) {
        continue;
      }
      const bool forward =
          timestamp_key(first.timestamp) < timestamp_key(second.timestamp);
      const auto &source = forward ? first : second;
      const auto &target = forward ? second : first;
      if (!source.clone_available || !target.clone_available) continue;
      output.push_back({source.camera_id, source.timestamp, target.timestamp,
                        attempt.feature_id, attempt.detached_index,
                        forward ? left : right,
                        forward ? right : left});
    }
  }
  std::sort(output.begin(), output.end(), candidate_key_less);
  return output;
}

struct CallbackCandidate {
  CandidateKey key;
  std::size_t attempt_index = 0U;
};

struct PairKey {
  std::size_t camera_id = 0U;
  double source = 0.0;
  double target = 0.0;
};

bool pair_key_less(const PairKey &left, const PairKey &right) {
  if (left.camera_id != right.camera_id)
    return left.camera_id < right.camera_id;
  const std::string left_source = timestamp_key(left.source);
  const std::string right_source = timestamp_key(right.source);
  if (left_source != right_source) return left_source < right_source;
  return timestamp_key(left.target) < timestamp_key(right.target);
}

const TurnSafeObservationValue *find_target_stereo(
    const TurnSafeFullTrackAttempt &attempt, const CandidateKey &candidate,
    std::uint64_t configured_camera_count) {
  if (configured_camera_count != 2U) return nullptr;
  for (const auto &observation : attempt.ordered_observations) {
    if (observation.camera_id != candidate.camera_id &&
        same_timestamp(observation.timestamp, candidate.target)) {
      return &observation;
    }
  }
  return nullptr;
}

std::string numeric_availability(bool available, double value,
                                 const char *reason) {
  if (available && std::isfinite(value)) {
    return std::string("{\"status\":\"AVAILABLE\",\"value\":") +
           double_json(value) + ",\"reason\":\"NONE\"}";
  }
  if (available) {
    return std::string("{\"status\":\"NONFINITE\",\"reason\":") +
           quote(std::isnan(value) ? "NATIVE_VALUE_NAN"
                                   : "NATIVE_VALUE_INFINITE") +
           "}";
  }
  return std::string("{\"status\":\"NOT_EXPOSED\",\"reason\":") +
         quote(reason) + "}";
}

std::string klt_error_summary_json(const std::vector<float> &values) {
  std::vector<double> finite;
  std::size_t nonfinite = 0U;
  for (float value : values) {
    if (std::isfinite(value)) finite.push_back(static_cast<double>(value));
    else ++nonfinite;
  }
  std::sort(finite.begin(), finite.end());
  std::ostringstream output;
  output << "{\"count\":" << finite.size()
         << ",\"nonfinite_count\":" << nonfinite;
  if (finite.empty()) {
    output << ",\"status\":\"NOT_APPLICABLE\",\"reason\":\"NO_FINITE_STATUS_SURVIVOR_ERRORS\"}";
    return output.str();
  }
  double sum = 0.0;
  for (double value : finite) sum += value;
  const auto nearest_rank = [&finite](double probability) {
    const std::size_t rank = static_cast<std::size_t>(
        std::ceil(probability * static_cast<double>(finite.size())));
    return finite[std::max<std::size_t>(1U, rank) - 1U];
  };
  output << ",\"status\":\"AVAILABLE\",\"min\":"
         << double_json(finite.front()) << ",\"max\":"
         << double_json(finite.back()) << ",\"mean\":"
         << double_json(sum / static_cast<double>(finite.size()))
         << ",\"p50\":" << double_json(nearest_rank(0.50))
         << ",\"p90\":" << double_json(nearest_rank(0.90))
         << ",\"p95\":" << double_json(nearest_rank(0.95))
         << ",\"p99\":" << double_json(nearest_rank(0.99)) << '}';
  return output.str();
}

const char *initializer_outcome_name(
    const TurnSafeInitializerValue &value) noexcept {
  const char *native_outcome = "NOT_ATTEMPTED";
  if (value.attempted) {
    if (value.native_success) native_outcome = "ACCEPTED";
    else if (value.predicate_ill_conditioned)
      native_outcome = "ILL_CONDITIONED";
    else if (value.predicate_too_near_or_behind)
      native_outcome = "TOO_NEAR_OR_BEHIND";
    else if (value.predicate_too_far)
      native_outcome = "TOO_FAR";
    else if (value.predicate_baseline_ratio)
      native_outcome = "BASELINE_RATIO";
    else if (value.predicate_native_nan)
      native_outcome = "NONFINITE";
    else native_outcome = "NATIVE_REJECTED_UNCLASSIFIED";
  }
  return native_outcome;
}

std::string initializer_json(const TurnSafeInitializerValue &value) {
  const char *native_outcome = initializer_outcome_name(value);
  const bool refinement_diagnostics =
      value.native_function == "single_gaussnewton";
  std::ostringstream output;
  output << "{\"attempted\":" << (value.attempted ? "true" : "false")
         << ",\"native_success\":" << (value.native_success ? "true" : "false")
         << ",\"native_function\":" << quote(value.native_function)
         << ",\"native_outcome\":" << quote(native_outcome)
         << ",\"condition_number\":"
         << numeric_availability(value.condition_available,
                                 value.condition_number,
                                 "NOT_EXPOSED_BY_NATIVE_PATH")
         << ",\"depth\":"
         << numeric_availability(value.depth_available, value.depth,
                                 "NOT_EXPOSED_BY_NATIVE_PATH")
         << ",\"baseline_ratio\":"
         << numeric_availability(value.baseline_ratio_available,
                                 value.baseline_ratio,
                                 "NOT_EXPOSED_BY_NATIVE_PATH")
         << ",\"predicates\":{\"ill_conditioned\":"
         << (value.predicate_ill_conditioned ? "true" : "false")
         << ",\"too_near_or_behind\":"
         << (value.predicate_too_near_or_behind ? "true" : "false")
         << ",\"too_far\":" << (value.predicate_too_far ? "true" : "false")
         << ",\"baseline_ratio\":"
         << (value.predicate_baseline_ratio ? "true" : "false")
         << ",\"native_nan\":" << (value.predicate_native_nan ? "true" : "false")
         << "},\"refinement_runs\":";
  if (refinement_diagnostics) output << value.refinement_runs;
  else output << "{\"status\":\"NOT_APPLICABLE\",\"reason\":\"NOT_A_REFINEMENT_STAGE\"}";
  output
         << ",\"refinement_lambda\":"
         << numeric_availability(refinement_diagnostics && value.attempted,
                                 value.refinement_lambda,
                                 "NOT_EXPOSED_BY_NATIVE_PATH")
         << ",\"refinement_last_step_norm\":"
         << numeric_availability(value.refinement_last_step_norm_available,
                                 value.refinement_last_step_norm,
                                 "NOT_EXPOSED_BY_NATIVE_PATH")
         << ",\"refinement_control_epsilon\":"
         << numeric_availability(refinement_diagnostics && value.attempted,
                                 value.refinement_control_epsilon,
                                 "NOT_EXPOSED_BY_NATIVE_PATH")
         << ",\"termination_reason\":"
         << quote(value.termination_reason)
         << '}';
  return output.str();
}

} // namespace

void set_turnsafe_diagnostic_fault_for_test(
    TurnSafeDiagnosticFaultStage stage,
    TurnSafeDiagnosticFaultKind kind) noexcept {
  diagnostic_fault_kind.store(static_cast<std::uint8_t>(kind),
                              std::memory_order_release);
  diagnostic_fault_stage.store(static_cast<std::uint8_t>(stage),
                               std::memory_order_release);
}

void clear_turnsafe_diagnostic_fault_for_test() noexcept {
  diagnostic_fault_stage.store(
      static_cast<std::uint8_t>(TurnSafeDiagnosticFaultStage::kNone),
      std::memory_order_release);
  diagnostic_fault_kind.store(
      static_cast<std::uint8_t>(TurnSafeDiagnosticFaultKind::kNone),
      std::memory_order_release);
}

void inject_turnsafe_diagnostic_fault_for_test(
    TurnSafeDiagnosticFaultStage stage) {
  inject_diagnostic_fault(stage);
}

const char *turnsafe_capture_disable_reason_name(
    TurnSafeCaptureDisableReason reason) noexcept {
  switch (reason) {
  case TurnSafeCaptureDisableReason::kNone: return "NONE";
  case TurnSafeCaptureDisableReason::kUnsupportedSchemaVersion:
    return "UNSUPPORTED_SCHEMA_VERSION";
  case TurnSafeCaptureDisableReason::kProvenanceConflict:
    return "PROVENANCE_CONFLICT";
  case TurnSafeCaptureDisableReason::kOutputOpenFailed:
    return "OUTPUT_OPEN_FAILED";
  case TurnSafeCaptureDisableReason::kOutputFdopenFailed:
    return "OUTPUT_FDOPEN_FAILED";
  case TurnSafeCaptureDisableReason::kOutputWriteFailed:
    return "OUTPUT_WRITE_FAILED";
  case TurnSafeCaptureDisableReason::kOutputRecordFlushFailed:
    return "OUTPUT_RECORD_FLUSH_FAILED";
  case TurnSafeCaptureDisableReason::kOutputFinalFlushOrFsyncFailed:
    return "OUTPUT_FINAL_FLUSH_OR_FSYNC_FAILED";
  case TurnSafeCaptureDisableReason::kOutputCloseFailed:
    return "OUTPUT_CLOSE_FAILED";
  case TurnSafeCaptureDisableReason::kOutputAtomicRenameFailed:
    return "OUTPUT_ATOMIC_RENAME_FAILED";
  case TurnSafeCaptureDisableReason::kOutputDirectoryFsyncFailed:
    return "OUTPUT_DIRECTORY_FSYNC_FAILED";
  case TurnSafeCaptureDisableReason::kNestedCallbackEnvelope:
    return "NESTED_CALLBACK_ENVELOPE";
  case TurnSafeCaptureDisableReason::kMultipleUpdaterRecords:
    return "MULTIPLE_UPDATER_RECORDS_PER_CALLBACK";
  case TurnSafeCaptureDisableReason::kUnterminatedCallbackEnvelope:
    return "UNTERMINATED_CALLBACK_ENVELOPE";
  case TurnSafeCaptureDisableReason::kInjectedWriteFailure:
    return "INJECTED_WRITE_FAILURE";
  case TurnSafeCaptureDisableReason::kDiagnosticBadAlloc:
    return "DIAGNOSTIC_BAD_ALLOC";
  case TurnSafeCaptureDisableReason::kDiagnosticStdException:
    return "DIAGNOSTIC_STD_EXCEPTION";
  case TurnSafeCaptureDisableReason::kDiagnosticUnknownException:
    return "DIAGNOSTIC_UNKNOWN_EXCEPTION";
  case TurnSafeCaptureDisableReason::kFrontendCaptureFailure:
    return "FRONTEND_CAPTURE_FAILURE";
  case TurnSafeCaptureDisableReason::kUpdaterCaptureFailure:
    return "UPDATER_CAPTURE_FAILURE";
  case TurnSafeCaptureDisableReason::kEventExtensionRequiresPassiveCapture:
    return "EVENT_EXTENSION_REQUIRES_ACTIVE_PASSIVE_CAPTURE";
  }
  return "DIAGNOSTIC_UNKNOWN_EXCEPTION";
}

const char *turnsafe_full_outcome_name(TurnSafeFullOutcome outcome) noexcept {
  switch (outcome) {
  case TurnSafeFullOutcome::kUnavailable: return "UNAVAILABLE";
  case TurnSafeFullOutcome::kFullAccepted: return "FULL_ACCEPTED";
  case TurnSafeFullOutcome::kInitIllConditioned: return "INIT_ILL_CONDITIONED";
  case TurnSafeFullOutcome::kInitTooNearOrBehind: return "INIT_TOO_NEAR_OR_BEHIND";
  case TurnSafeFullOutcome::kInitTooFar: return "INIT_TOO_FAR";
  case TurnSafeFullOutcome::kInitNonfinite: return "INIT_NONFINITE";
  case TurnSafeFullOutcome::kInitBaselineRatio: return "INIT_BASELINE_RATIO";
  case TurnSafeFullOutcome::kRefinementFailed: return "REFINEMENT_FAILED";
  case TurnSafeFullOutcome::kSchurInsufficientRows: return "SCHUR_INSUFFICIENT_ROWS";
  case TurnSafeFullOutcome::kSchurRankDeficient: return "SCHUR_RANK_DEFICIENT";
  case TurnSafeFullOutcome::kSchurIllConditioned: return "SCHUR_ILL_CONDITIONED";
  case TurnSafeFullOutcome::kSchurNonfinite: return "SCHUR_NONFINITE";
  case TurnSafeFullOutcome::kFullNisRejected: return "FULL_NIS_REJECTED";
  case TurnSafeFullOutcome::kUnsupportedOrInvalidInput: return "UNSUPPORTED_OR_INVALID_INPUT";
  }
  return "UNAVAILABLE";
}

const char *turnsafe_outcome_mapping_name(TurnSafeOutcomeMapping mapping) noexcept {
  switch (mapping) {
  case TurnSafeOutcomeMapping::kExact: return "EXACT";
  case TurnSafeOutcomeMapping::kUnmapped: return "UNMAPPED";
  case TurnSafeOutcomeMapping::kNotApplicable: return "NOT_APPLICABLE";
  }
  return "UNMAPPED";
}

bool turnsafe_shadow_eligible_outcome(TurnSafeFullOutcome outcome) noexcept {
  return outcome == TurnSafeFullOutcome::kInitIllConditioned ||
         outcome == TurnSafeFullOutcome::kInitTooFar ||
         outcome == TurnSafeFullOutcome::kInitBaselineRatio ||
         outcome == TurnSafeFullOutcome::kSchurRankDeficient ||
         outcome == TurnSafeFullOutcome::kSchurIllConditioned;
}

TurnSafeResolvedConfiguration TurnSafeDiagnostics::EvaluateConfiguration(
    TurnSafeResolvedConfiguration configuration) {
  configuration.unsupported_reasons.clear();
  if (!configuration.one_pass_schur)
    configuration.unsupported_reasons.push_back("ONE_PASS_SCHUR_REQUIRED");
  if (!configuration.fej_enabled)
    configuration.unsupported_reasons.push_back("FEJ_REQUIRED");
  if (!configuration.global_3d_transient)
    configuration.unsupported_reasons.push_back("GLOBAL_3D_REQUIRED");
  if (!configuration.all_cameras_radtan)
    configuration.unsupported_reasons.push_back("CAMRADTAN_REQUIRED");
  if (!configuration.camera_extrinsic_calibration_off)
    configuration.unsupported_reasons.push_back(
        "CAMERA_EXTRINSIC_CALIBRATION_MUST_BE_OFF");
  if (!configuration.camera_intrinsic_calibration_off)
    configuration.unsupported_reasons.push_back(
        "CAMERA_INTRINSIC_CALIBRATION_MUST_BE_OFF");
  if (!configuration.camera_time_offset_calibration_off)
    configuration.unsupported_reasons.push_back(
        "CAMERA_TIME_OFFSET_CALIBRATION_MUST_BE_OFF");
  if (!configuration.stereo_enabled)
    configuration.unsupported_reasons.push_back("STEREO_MUST_BE_ENABLED");
  if (!configuration.stereo_available || configuration.camera_count != 2U)
    configuration.unsupported_reasons.push_back(
        "EXACT_TWO_CAMERA_STEREO_MUST_BE_AVAILABLE");
  if (!configuration.require_target_stereo_range)
    configuration.unsupported_reasons.push_back(
        "TARGET_STEREO_RANGE_REQUIREMENT_MUST_BE_CONFIGURED");
  std::sort(configuration.unsupported_reasons.begin(),
            configuration.unsupported_reasons.end());
  configuration.unsupported_reasons.erase(
      std::unique(configuration.unsupported_reasons.begin(),
                  configuration.unsupported_reasons.end()),
      configuration.unsupported_reasons.end());
  configuration.supported = configuration.unsupported_reasons.empty();
  return configuration;
}

std::shared_ptr<TurnSafeDiagnostics> TurnSafeDiagnostics::Create(
    const TurnSafeDiagnosticsOptions &options) noexcept {
  const bool extension_requested =
      options.capture_causal_imu_intervals ||
      options.capture_outcome_association_keys ||
      options.capture_group_bearing_provenance;
  if (extension_requested &&
      (!options.capture_requested || options.output_path.empty())) {
    try {
      auto sink = std::shared_ptr<TurnSafeDiagnostics>(
          new TurnSafeDiagnostics(options));
      sink->Disable(
          TurnSafeCaptureDisableReason::kEventExtensionRequiresPassiveCapture);
      return sink;
    } catch (...) {
      return nullptr;
    }
  }
  if (!options.capture_requested || options.output_path.empty()) return nullptr;
  try {
    TurnSafeDiagnosticsOptions bound = options;
    // Recompute support from raw resolved predicates at the trusted sink
    // boundary. Caller-provided `supported` and reason strings are never
    // accepted as evidence.
    bound.resolved_configuration =
        EvaluateConfiguration(std::move(bound.resolved_configuration));
    bound.source_sha = turnsafe_build::kSourceSha;
    bound.source_tree = turnsafe_build::kSourceTree;
    bound.source_snapshot_sha256 =
        turnsafe_build::kSourceSnapshotSha256;
    bound.source_dirty = turnsafe_build::kSourceDirty;
    bound.build_provenance_id = turnsafe_build::kBuildProvenanceId;
    bound.configure_manifest_sha256 =
        turnsafe_build::kConfigureManifestSha256;
    const bool source_sha_conflict =
        !bound.expected_source_sha.empty() &&
        bound.expected_source_sha != bound.source_sha;
    const bool source_tree_conflict =
        !bound.expected_source_tree.empty() &&
        bound.expected_source_tree != bound.source_tree;
    const bool snapshot_conflict =
        !bound.expected_source_snapshot_sha256.empty() &&
        bound.expected_source_snapshot_sha256 !=
            bound.source_snapshot_sha256;
    const bool build_id_conflict =
        !bound.expected_build_provenance_id.empty() &&
        bound.expected_build_provenance_id != bound.build_provenance_id;
    const bool schema_conflict =
        !bound.diagnostic_schema_sha256.empty() &&
        bound.diagnostic_schema_sha256 !=
            turnsafe_build::kDiagnosticSchemaSha256;
    bound.provenance_conflict = bound.provenance_conflict ||
        source_sha_conflict || source_tree_conflict || snapshot_conflict ||
        build_id_conflict || schema_conflict;
    bound.diagnostic_schema_sha256 =
        turnsafe_build::kDiagnosticSchemaSha256;
    auto sink = std::shared_ptr<TurnSafeDiagnostics>(
        new TurnSafeDiagnostics(bound));
    sink->Initialize();
    return sink;
  } catch (...) {
    return nullptr;
  }
}

TurnSafeDiagnostics::~TurnSafeDiagnostics() noexcept { Finalize(); }

bool TurnSafeDiagnostics::active() const noexcept {
  return active_.load(std::memory_order_acquire);
}

const char *TurnSafeDiagnostics::failure_reason() const noexcept {
  return turnsafe_capture_disable_reason_name(
      static_cast<TurnSafeCaptureDisableReason>(
          failure_reason_.load(std::memory_order_acquire)));
}

std::uint64_t TurnSafeDiagnostics::failure_count() const noexcept {
  return failure_count_.load(std::memory_order_acquire);
}

bool TurnSafeDiagnostics::Initialize() noexcept {
  int descriptor = -1;
  try {
    if (options_.schema_version != "turnsafe.t0.v1") {
      Disable(TurnSafeCaptureDisableReason::kUnsupportedSchemaVersion);
      return false;
    }
    if (EventExtensionRequested() &&
        options_.event_extension_schema_version !=
            "turnsafe.t0.event_extension.v1") {
      Disable(TurnSafeCaptureDisableReason::kUnsupportedSchemaVersion);
      return false;
    }
    if (options_.provenance_conflict) {
      Disable(TurnSafeCaptureDisableReason::kProvenanceConflict);
      return false;
    }
    temporary_path_ = options_.output_path + ".tmp";
    inject_diagnostic_fault(TurnSafeDiagnosticFaultStage::kSinkOpen);
    descriptor = ::open(temporary_path_.c_str(),
                        O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC,
                        S_IRUSR | S_IWUSR | S_IRGRP | S_IROTH);
    if (descriptor < 0) {
      Disable(TurnSafeCaptureDisableReason::kOutputOpenFailed);
      return false;
    }
    inject_diagnostic_fault(TurnSafeDiagnosticFaultStage::kSinkFdopen);
    stream_ = ::fdopen(descriptor, "wb");
    if (stream_ == nullptr) {
      ::close(descriptor);
      descriptor = -1;
      Disable(TurnSafeCaptureDisableReason::kOutputFdopenFailed);
      return false;
    }
    descriptor = -1;
    active_.store(true, std::memory_order_release);
    inject_diagnostic_fault(
        TurnSafeDiagnosticFaultStage::kHeaderSerialization);
    if (!WriteLine(SerializeHeader())) return false;
    return true;
  } catch (const std::bad_alloc &) {
    if (descriptor >= 0) ::close(descriptor);
    Disable(TurnSafeCaptureDisableReason::kDiagnosticBadAlloc);
    return false;
  } catch (const std::exception &) {
    if (descriptor >= 0) ::close(descriptor);
    Disable(TurnSafeCaptureDisableReason::kDiagnosticStdException);
    return false;
  } catch (...) {
    if (descriptor >= 0) ::close(descriptor);
    Disable(TurnSafeCaptureDisableReason::kDiagnosticUnknownException);
    return false;
  }
}

bool TurnSafeDiagnostics::EventExtensionRequested() const noexcept {
  return options_.capture_causal_imu_intervals ||
         options_.capture_outcome_association_keys ||
         options_.capture_group_bearing_provenance;
}

void TurnSafeDiagnostics::Disable(
    TurnSafeCaptureDisableReason reason) noexcept {
  active_.store(false, std::memory_order_release);
  std::uint8_t expected =
      static_cast<std::uint8_t>(TurnSafeCaptureDisableReason::kNone);
  failure_reason_.compare_exchange_strong(
      expected, static_cast<std::uint8_t>(reason), std::memory_order_acq_rel,
      std::memory_order_acquire);
  std::uint64_t count = failure_count_.load(std::memory_order_relaxed);
  while (count != std::numeric_limits<std::uint64_t>::max() &&
         !failure_count_.compare_exchange_weak(
             count, count + 1U, std::memory_order_relaxed,
             std::memory_order_relaxed)) {
  }
}

void TurnSafeDiagnostics::DisableForCurrentException() noexcept {
  try {
    throw;
  } catch (const std::bad_alloc &) {
    Disable(TurnSafeCaptureDisableReason::kDiagnosticBadAlloc);
  } catch (const std::exception &) {
    Disable(TurnSafeCaptureDisableReason::kDiagnosticStdException);
  } catch (...) {
    Disable(TurnSafeCaptureDisableReason::kDiagnosticUnknownException);
  }
}

bool TurnSafeDiagnostics::WriteLine(const std::string &line) noexcept {
  try {
    if (!active_.load(std::memory_order_acquire) || stream_ == nullptr)
      return false;
    if (callbacks_written_ >= options_.test_fail_after_callback_records) {
      Disable(TurnSafeCaptureDisableReason::kInjectedWriteFailure);
      return false;
    }
    inject_diagnostic_fault(TurnSafeDiagnosticFaultStage::kSinkWrite);
    if (std::fwrite(line.data(), 1U, line.size(), stream_) != line.size() ||
        std::fputc('\n', stream_) == EOF) {
      Disable(TurnSafeCaptureDisableReason::kOutputWriteFailed);
      return false;
    }
    inject_diagnostic_fault(TurnSafeDiagnosticFaultStage::kSinkRecordFlush);
    if (std::fflush(stream_) != 0) {
      Disable(TurnSafeCaptureDisableReason::kOutputRecordFlushFailed);
      return false;
    }
    return true;
  } catch (...) {
    DisableForCurrentException();
    return false;
  }
}

std::string TurnSafeDiagnostics::SerializeHeader() const {
  std::ostringstream output;
  std::vector<std::string> reasons =
      options_.resolved_configuration.unsupported_reasons;
  std::sort(reasons.begin(), reasons.end());
  reasons.erase(std::unique(reasons.begin(), reasons.end()), reasons.end());
  output << "{\"schema\":\"turnsafe.t0.v1\",\"record_type\":\"run_header\""
         << ",\"frozen_base_sha\":" << quote(options_.frozen_base_sha)
         << ",\"source_sha\":" << quote(options_.source_sha)
         << ",\"tree_sha\":" << quote(options_.source_tree)
         << ",\"source_snapshot_sha256\":"
         << quote(options_.source_snapshot_sha256)
         << ",\"source_dirty\":"
         << (options_.source_dirty ? "true" : "false")
         << ",\"build_provenance_id\":"
         << quote(options_.build_provenance_id)
         << ",\"configure_manifest_sha256\":"
         << quote(options_.configure_manifest_sha256)
         << ",\"build_manifest_sha256\":" << quote(options_.build_manifest_sha256)
         << ",\"binary_sha256\":" << quote(options_.binary_sha256)
         << ",\"config_sha256\":" << quote(options_.config_sha256)
         << ",\"calibration_sha256\":" << quote(options_.calibration_sha256)
         << ",\"diagnostic_schema_sha256\":" << quote(options_.diagnostic_schema_sha256)
         << ",\"capture_mode\":\"capture_only\",\"supported_configuration\":"
         << (options_.resolved_configuration.supported ? "true" : "false")
         << ",\"unsupported_reasons\":[";
  for (std::size_t index = 0U; index < reasons.size(); ++index) {
    if (index != 0U) output << ',';
    output << quote(reasons[index]);
  }
  const TurnSafeResolvedConfiguration &configuration =
      options_.resolved_configuration;
  output << ']'
         << ",\"resolved_configuration\":{\"one_pass_schur\":"
         << (configuration.one_pass_schur ? "true" : "false")
         << ",\"fej_enabled\":"
         << (configuration.fej_enabled ? "true" : "false")
         << ",\"global_3d_transient\":"
         << (configuration.global_3d_transient ? "true" : "false")
         << ",\"all_cameras_radtan\":"
         << (configuration.all_cameras_radtan ? "true" : "false")
         << ",\"camera_extrinsic_calibration_off\":"
         << (configuration.camera_extrinsic_calibration_off ? "true" : "false")
         << ",\"camera_intrinsic_calibration_off\":"
         << (configuration.camera_intrinsic_calibration_off ? "true" : "false")
         << ",\"camera_time_offset_calibration_off\":"
         << (configuration.camera_time_offset_calibration_off ? "true" : "false")
         << ",\"stereo_enabled\":"
         << (configuration.stereo_enabled ? "true" : "false")
         << ",\"stereo_available\":"
         << (configuration.stereo_available ? "true" : "false")
         << ",\"require_target_stereo_range\":"
         << (configuration.require_target_stereo_range ? "true" : "false")
         << ",\"camera_count\":" << configuration.camera_count << '}'
         << ",\"threshold_set_id\":{\"status\":\"NOT_APPLICABLE\",\"reason\":\"THRESHOLD_SET_NOT_FROZEN\"}"
         << ",\"digest_contract_version\":" << quote(options_.digest_contract_version);
  if (EventExtensionRequested()) {
    output << ",\"extensions\":{\"event_extension\":{\"schema\":"
           << quote(options_.event_extension_schema_version)
           << ",\"record_version\":1"
           << ",\"capture_flags\":{\"causal_imu_intervals\":"
           << (options_.capture_causal_imu_intervals ? "true" : "false")
           << ",\"outcome_association_keys\":"
           << (options_.capture_outcome_association_keys ? "true" : "false")
           << ",\"group_bearing_provenance\":"
           << (options_.capture_group_bearing_provenance ? "true" : "false")
           << "},\"integration_method\":\"piecewise_linear_trapezoid.v1\""
           << ",\"rotation_convention\":\"native_gyroscope_frame_passive_left_exp_minus_omega_diagnostic.v1\""
           << ",\"gyro_bias_convention\":\"raw_wm_minus_current_callback_bias_g.v1\""
           << ",\"clock_mapping\":\"t_imu_equals_t_camera_plus_dt_CAMtoIMU.v1\""
           << ",\"association_version\":\"turnsafe.offline_association.v1\""
           << ",\"canonical_ordering_version\":\"turnsafe.event_extension.order.v1\""
           << ",\"image_cell_representation\":\"continuous_pixel_center_fraction.v1\""
           << ",\"group_hash_algorithm\":\"fnv1a64_binary64_be.v1\"}}";
  }
  output << '}';
  return output.str();
}

TurnSafeCausalImuInterval TurnSafeDiagnostics::EvaluateCausalImuInterval(
    std::uint64_t interval_id, const std::vector<int> &camera_frame_ids,
    bool previous_timestamp_available, double previous_timestamp,
    double current_timestamp, bool force_initialization_boundary,
    bool imu_time_offset_available, double imu_time_offset_s,
    bool gyro_bias_available,
    const std::array<double, 3U> &gyro_bias_rad_s,
    const TurnSafeImuSupportSnapshot &support) {
  TurnSafeCausalImuInterval output;
  output.gyro_interval_id = interval_id;
  output.camera_frame_ids = camera_frame_ids;
  std::sort(output.camera_frame_ids.begin(), output.camera_frame_ids.end());
  output.camera_frame_ids.erase(
      std::unique(output.camera_frame_ids.begin(),
                  output.camera_frame_ids.end()),
      output.camera_frame_ids.end());
  output.previous_callback_timestamp_available = previous_timestamp_available;
  output.previous_callback_timestamp = previous_timestamp;
  output.current_callback_timestamp = current_timestamp;
  output.imu_time_offset_available =
      imu_time_offset_available && std::isfinite(imu_time_offset_s);
  output.imu_time_offset_reason = output.imu_time_offset_available
                                      ? "NONE"
                                      : (imu_time_offset_available
                                             ? "IMU_TIME_OFFSET_NONFINITE"
                                             : "IMU_TIME_OFFSET_UNAVAILABLE");
  output.imu_time_offset_s = imu_time_offset_s;
  output.gyro_bias_available =
      gyro_bias_available && finite_vector3(gyro_bias_rad_s);
  output.gyro_bias_reason = output.gyro_bias_available
                                ? "NONE"
                                : (gyro_bias_available ? "BIAS_NONFINITE"
                                                       : "BIAS_UNAVAILABLE");
  output.gyro_bias_rad_s = gyro_bias_rad_s;

  if (!previous_timestamp_available) {
    output.reason = "FIRST_CALLBACK";
    output.source_sample_count_reason = output.reason;
    return output;
  }
  if (force_initialization_boundary) {
    output.reason = "FIRST_CALLBACK_AFTER_INITIALIZATION";
    output.source_sample_count_reason = output.reason;
    return output;
  }
  if (!std::isfinite(previous_timestamp) ||
      !std::isfinite(current_timestamp)) {
    output.reason = "CALLBACK_TIMESTAMP_NONFINITE";
    output.source_sample_count_reason = output.reason;
    return output;
  }
  if (!(current_timestamp > previous_timestamp)) {
    output.reason = "CALLBACK_TIMESTAMP_REGRESSION";
    output.source_sample_count_reason = output.reason;
    return output;
  }
  output.duration_available = true;
  output.duration_s = current_timestamp - previous_timestamp;
  if (!imu_time_offset_available) {
    output.reason = "IMU_TIME_OFFSET_UNAVAILABLE";
    output.source_sample_count_reason = output.reason;
    return output;
  }
  if (!std::isfinite(imu_time_offset_s)) {
    output.reason = "IMU_TIME_OFFSET_NONFINITE";
    output.source_sample_count_reason = output.reason;
    return output;
  }
  output.imu_interval_start_s = previous_timestamp + imu_time_offset_s;
  output.imu_interval_end_s = current_timestamp + imu_time_offset_s;
  output.imu_interval_endpoints_available =
      std::isfinite(output.imu_interval_start_s) &&
      std::isfinite(output.imu_interval_end_s);
  if (!output.imu_interval_endpoints_available) {
    output.reason = "IMU_INTERVAL_ENDPOINT_NONFINITE";
    output.source_sample_count_reason = output.reason;
    return output;
  }
  if (!support.available) {
    output.reason = support.reason.empty() ? "NO_CAUSAL_BUFFERED_IMU"
                                            : support.reason;
    output.source_sample_count_reason = output.reason;
    return output;
  }
  output.source_sample_count_available = true;
  output.source_sample_count_reason = "NONE";
  output.source_sample_count =
      static_cast<std::uint64_t>(support.samples.size());
  if (support.samples.empty()) {
    output.reason = "NO_CAUSAL_BUFFERED_IMU";
    return output;
  }
  for (std::size_t index = 0U; index < support.samples.size(); ++index) {
    const auto &sample = support.samples[index];
    if (!std::isfinite(sample.timestamp)) {
      output.reason = "IMU_TIMESTAMP_NONFINITE";
      return output;
    }
    if (!finite_vector3(sample.omega_rad_s)) {
      output.reason = "RAW_GYRO_NONFINITE";
      return output;
    }
    if (index != 0U &&
        !(sample.timestamp > support.samples[index - 1U].timestamp)) {
      output.reason = "IMU_TIMESTAMP_NOT_STRICTLY_INCREASING";
      return output;
    }
  }
  output.first_support_timestamp_available = true;
  output.first_support_timestamp = support.samples.front().timestamp;
  output.last_support_timestamp_available = true;
  output.last_support_timestamp = support.samples.back().timestamp;
  const double overlap_start =
      std::max(output.imu_interval_start_s, support.samples.front().timestamp);
  const double overlap_end =
      std::min(output.imu_interval_end_s, support.samples.back().timestamp);
  const double overlap = std::max(0.0, overlap_end - overlap_start);
  output.coverage_fraction_available = true;
  output.coverage_fraction =
      std::max(0.0, std::min(1.0, overlap / output.duration_s));

  const auto endpoint = [&support](double timestamp,
                                   TurnSafeImuEndpointEvidence &evidence,
                                   std::array<double, 3U> &omega) {
    auto upper = std::lower_bound(
        support.samples.begin(), support.samples.end(), timestamp,
        [](const TurnSafeImuSampleValue &sample, double time) {
          return sample.timestamp < time;
        });
    if (upper != support.samples.end() && upper->timestamp == timestamp) {
      evidence.status = "EXACT_SAMPLE";
      evidence.reason = "NONE";
      evidence.lower_timestamp_available = true;
      evidence.upper_timestamp_available = true;
      evidence.lower_timestamp = upper->timestamp;
      evidence.upper_timestamp = upper->timestamp;
      omega = upper->omega_rad_s;
      return true;
    }
    if (upper == support.samples.begin() || upper == support.samples.end()) {
      if (upper != support.samples.end()) {
        evidence.upper_timestamp_available = true;
        evidence.upper_timestamp = upper->timestamp;
      }
      if (upper != support.samples.begin()) {
        const auto lower = upper - 1;
        evidence.lower_timestamp_available = true;
        evidence.lower_timestamp = lower->timestamp;
      }
      evidence.status = "UNSUPPORTED";
      evidence.reason = "ENDPOINT_BRACKET_UNAVAILABLE";
      return false;
    }
    const auto lower = upper - 1;
    evidence.lower_timestamp_available = true;
    evidence.upper_timestamp_available = true;
    evidence.lower_timestamp = lower->timestamp;
    evidence.upper_timestamp = upper->timestamp;
    evidence.status = "LINEAR_INTERPOLATED";
    evidence.reason = "NONE";
    omega = interpolate_omega(*lower, *upper, timestamp);
    return true;
  };

  std::array<double, 3U> start_omega{{0.0, 0.0, 0.0}};
  std::array<double, 3U> end_omega{{0.0, 0.0, 0.0}};
  inject_diagnostic_fault(TurnSafeDiagnosticFaultStage::kEndpointInterpolation);
  const bool start_available = endpoint(output.imu_interval_start_s,
                                        output.start_endpoint, start_omega);
  const bool end_available = endpoint(output.imu_interval_end_s,
                                      output.end_endpoint, end_omega);
  if (!start_available) {
    output.start_endpoint.reason = "START_ENDPOINT_UNSUPPORTED";
    output.reason = "START_ENDPOINT_UNSUPPORTED";
    return output;
  }
  if (!end_available) {
    output.end_endpoint.reason = "END_ENDPOINT_UNSUPPORTED";
    output.reason = "END_ENDPOINT_UNSUPPORTED";
    return output;
  }

  output.knot_timestamps_s.clear();
  output.raw_omega_rad_s.clear();
  output.bias_corrected_omega_rad_s.clear();
  output.knot_timestamps_s.push_back(output.imu_interval_start_s);
  output.raw_omega_rad_s.push_back(start_omega);
  for (const auto &sample : support.samples) {
    if (sample.timestamp > output.imu_interval_start_s &&
        sample.timestamp < output.imu_interval_end_s) {
      output.knot_timestamps_s.push_back(sample.timestamp);
      output.raw_omega_rad_s.push_back(sample.omega_rad_s);
    }
  }
  output.knot_timestamps_s.push_back(output.imu_interval_end_s);
  output.raw_omega_rad_s.push_back(end_omega);
  if (output.knot_timestamps_s.size() >= 2U) {
    output.maximum_internal_gap_s = 0.0;
    for (std::size_t index = 0U;
         index + 1U < output.knot_timestamps_s.size(); ++index) {
      output.maximum_internal_gap_s = std::max(
          output.maximum_internal_gap_s,
          output.knot_timestamps_s[index + 1U] -
              output.knot_timestamps_s[index]);
    }
    output.maximum_internal_gap_available =
        std::isfinite(output.maximum_internal_gap_s);
  }
  inject_diagnostic_fault(TurnSafeDiagnosticFaultStage::kGyroIntegration);
  output.raw_summary = summarize_gyro_series(output.knot_timestamps_s,
                                             output.raw_omega_rad_s);
  if (output.gyro_bias_available) {
    output.bias_corrected_omega_rad_s.reserve(
        output.raw_omega_rad_s.size());
    for (const auto &raw : output.raw_omega_rad_s) {
      output.bias_corrected_omega_rad_s.push_back(
          {{raw[0] - gyro_bias_rad_s[0], raw[1] - gyro_bias_rad_s[1],
            raw[2] - gyro_bias_rad_s[2]}});
    }
    output.bias_corrected_summary = summarize_gyro_series(
        output.knot_timestamps_s, output.bias_corrected_omega_rad_s);
  } else {
    output.bias_corrected_summary.reason = output.gyro_bias_reason;
  }
  if (!output.raw_summary.available) {
    output.reason = output.raw_summary.reason;
    return output;
  }
  if (!output.gyro_bias_available) {
    output.reason = output.gyro_bias_reason;
    return output;
  }
  if (!output.bias_corrected_summary.available) {
    output.reason = output.bias_corrected_summary.reason;
    return output;
  }
  output.available = true;
  output.reason = "NONE";
  return output;
}

void TurnSafeDiagnostics::BeginCallback(double timestamp) noexcept {
  BeginCallbackImpl(timestamp, nullptr, false, false, nullptr, nullptr);
}

void TurnSafeDiagnostics::BeginCallback(
    double timestamp, const std::vector<int> &camera_frame_ids,
    bool estimator_initialized, bool output_ready, const State *state,
    Propagator *propagator) noexcept {
  BeginCallbackImpl(timestamp, &camera_frame_ids, estimator_initialized,
                    output_ready, state, propagator);
}

void TurnSafeDiagnostics::BeginCallbackImpl(
    double timestamp, const std::vector<int> *camera_frame_ids,
    bool estimator_initialized, bool output_ready, const State *state,
    Propagator *propagator) noexcept {
  try {
    std::uint64_t interval_id = 0U;
    bool previous_timestamp_available = false;
    double previous_timestamp = 0.0;
    bool initialization_boundary = false;
    {
      const std::lock_guard<std::mutex> lock(mutex_);
      if (!active_.load(std::memory_order_acquire)) return;
      if (current_available_) {
        Disable(TurnSafeCaptureDisableReason::kNestedCallbackEnvelope);
        return;
      }
      current_ = CallbackRecord();
      current_.callback_index = next_callback_index_++;
      current_.timestamp = timestamp;
      current_.event_extension_available = EventExtensionRequested();
      current_.initialized_before_available = camera_frame_ids != nullptr;
      current_.initialized_before = estimator_initialized;
      current_available_ = true;
      interval_id = current_.callback_index;
      previous_timestamp_available = previous_processed_callback_available_;
      previous_timestamp = previous_processed_callback_timestamp_;
      initialization_boundary = next_callback_initialization_boundary_;
      next_callback_initialization_boundary_ = false;
      if (options_.capture_outcome_association_keys) {
        current_.outcome_association_keys.initialized_before_available = true;
        current_.outcome_association_keys.initialized_before =
            estimator_initialized;
      }
    }

    if (options_.capture_outcome_association_keys && state != nullptr) {
      inject_diagnostic_fault(TurnSafeDiagnosticFaultStage::kCallbackMetadata);
      TurnSafeOutcomeAssociationKeys metadata;
      metadata.initialized_before_available = true;
      metadata.initialized_before = estimator_initialized;
      metadata.state_timestamp_before_available =
          std::isfinite(state->_timestamp);
      metadata.state_timestamp_before_reason =
          metadata.state_timestamp_before_available ? "NONE"
                                                    : "STATE_TIMESTAMP_NONFINITE";
      metadata.state_timestamp_before = state->_timestamp;
      metadata.estimator_valid_before_available = state->_imu != nullptr;
      metadata.estimator_valid_before =
          output_ready && state->_imu != nullptr &&
          state->_imu->value().allFinite() &&
          std::isfinite(state->_timestamp);
      const std::lock_guard<std::mutex> lock(mutex_);
      if (active_.load(std::memory_order_acquire) && current_available_ &&
          current_.callback_index == interval_id) {
        current_.outcome_association_keys = metadata;
      }
    }

    if (options_.capture_causal_imu_intervals) {
      bool offset_available = false;
      double offset = std::numeric_limits<double>::quiet_NaN();
      bool bias_available = false;
      std::array<double, 3U> bias{{0.0, 0.0, 0.0}};
      if (state != nullptr && state->_calib_dt_CAMtoIMU != nullptr) {
        offset = state->_calib_dt_CAMtoIMU->value()(0);
        offset_available = true;
      }
      if (state != nullptr && state->_imu != nullptr) {
        const Eigen::Vector3d state_bias = state->_imu->bias_g();
        bias = {{state_bias(0), state_bias(1), state_bias(2)}};
        bias_available = true;
      }
      TurnSafeImuSupportSnapshot support;
      if (!previous_timestamp_available) {
        support.reason = "FIRST_CALLBACK";
      } else if (initialization_boundary) {
        support.reason = "FIRST_CALLBACK_AFTER_INITIALIZATION";
      } else if (propagator == nullptr) {
        support.reason = "IMU_BUFFER_NOT_EXPOSED";
      } else if (offset_available && std::isfinite(offset) &&
                 std::isfinite(previous_timestamp) &&
                 std::isfinite(timestamp) && timestamp > previous_timestamp) {
        inject_diagnostic_fault(TurnSafeDiagnosticFaultStage::kImuSupportCopy);
        support = propagator->turnsafe_copy_imu_support(
            previous_timestamp + offset, timestamp + offset);
      } else {
        support.reason = "INTERVAL_PREREQUISITE_UNAVAILABLE";
      }
      const std::vector<int> empty_ids;
      TurnSafeCausalImuInterval interval = EvaluateCausalImuInterval(
          interval_id,
          camera_frame_ids == nullptr ? empty_ids : *camera_frame_ids,
          previous_timestamp_available, previous_timestamp, timestamp,
          initialization_boundary, offset_available, offset, bias_available,
          bias, support);
      const std::lock_guard<std::mutex> lock(mutex_);
      if (active_.load(std::memory_order_acquire) && current_available_ &&
          current_.callback_index == interval_id) {
        current_.causal_imu_interval = std::move(interval);
      }
    }
  } catch (...) {
    DisableForCurrentException();
  }
}

void TurnSafeDiagnostics::RecordFrontend(
    ov_core::TrackKLTFrameDiagnostics &&record) noexcept {
  try {
    inject_diagnostic_fault(TurnSafeDiagnosticFaultStage::kFrontendCopy);
    const std::lock_guard<std::mutex> lock(mutex_);
    if (!active_.load(std::memory_order_acquire) || !current_available_) return;
    current_.frontend.push_back(std::move(record));
  } catch (...) {
    DisableForCurrentException();
  }
}

void TurnSafeDiagnostics::RecordUpdate(TurnSafeUpdateRecord &&record) noexcept {
  try {
    inject_diagnostic_fault(TurnSafeDiagnosticFaultStage::kUpdateCopy);
    const std::lock_guard<std::mutex> lock(mutex_);
    if (!active_.load(std::memory_order_acquire) || !current_available_) return;
    if (current_.update_available) {
      Disable(TurnSafeCaptureDisableReason::kMultipleUpdaterRecords);
      return;
    }
    current_.update = std::move(record);
    current_.update_available = true;
  } catch (...) {
    DisableForCurrentException();
  }
}

void TurnSafeDiagnostics::EndCallback() noexcept {
  EndCallbackImpl(false, false, false, nullptr, false);
}

void TurnSafeDiagnostics::EndCallback(bool estimator_initialized,
                                      bool output_ready, const State *state,
                                      bool callback_incomplete) noexcept {
  EndCallbackImpl(true, estimator_initialized, output_ready, state,
                  callback_incomplete);
}

void TurnSafeDiagnostics::EndCallbackImpl(bool initialized_available,
                                          bool estimator_initialized,
                                          bool output_ready,
                                          const State *state,
                                          bool callback_incomplete) noexcept {
  try {
    TurnSafeOutcomeAssociationKeys after;
    bool after_metadata_available = false;
    double callback_timestamp = std::numeric_limits<double>::quiet_NaN();
    {
      const std::lock_guard<std::mutex> lock(mutex_);
      if (!active_.load(std::memory_order_acquire) || !current_available_)
        return;
      callback_timestamp = current_.timestamp;
    }
    if (options_.capture_outcome_association_keys) {
      inject_diagnostic_fault(TurnSafeDiagnosticFaultStage::kCallbackMetadata);
      after.initialized_after_available = initialized_available;
      after.initialized_after = estimator_initialized;
      after.callback_incomplete = callback_incomplete;
      after.callback_completion_reason =
          callback_incomplete ? "EXCEPTION_SCOPE_EXIT" : "NORMAL_SCOPE_EXIT";
      if (state != nullptr) {
        after.state_timestamp_after_available =
            std::isfinite(state->_timestamp);
        after.state_timestamp_after_reason =
            after.state_timestamp_after_available ? "NONE"
                                                  : "STATE_TIMESTAMP_NONFINITE";
        after.state_timestamp_after = state->_timestamp;
        after.estimator_valid_after_available = state->_imu != nullptr;
        after.estimator_valid_after =
            output_ready && state->_imu != nullptr &&
            state->_imu->value().allFinite() &&
            std::isfinite(state->_timestamp);
        after.nonfinite_observed_available = state->_imu != nullptr;
        after.nonfinite_observed_reason =
            after.nonfinite_observed_available ? "NONE"
                                               : "NOMINAL_IMU_NOT_EXPOSED";
        after.nonfinite_observed = !std::isfinite(state->_timestamp) ||
            (state->_imu != nullptr && !state->_imu->value().allFinite());
        after.expected_output_timestamp_reason =
            "EXPECTED_OUTPUT_TIMESTAMP_UNAVAILABLE";
        if (!initialized_available || !estimator_initialized) {
          after.expected_output_timestamp_reason = "ESTIMATOR_NOT_INITIALIZED";
        } else if (!output_ready) {
          after.expected_output_timestamp_reason = "OUTPUT_NOT_READY";
        } else if (callback_incomplete) {
          after.expected_output_timestamp_reason = "CALLBACK_INCOMPLETE";
        } else if (!after.estimator_valid_after) {
          after.expected_output_timestamp_reason = "ESTIMATOR_STATE_INVALID";
        } else if (!same_timestamp(state->_timestamp, callback_timestamp)) {
          after.expected_output_timestamp_reason =
              "STATE_TIMESTAMP_NOT_CURRENT_CALLBACK";
        } else if (state->_calib_dt_CAMtoIMU == nullptr) {
          after.expected_output_timestamp_reason = "IMU_TIME_OFFSET_UNAVAILABLE";
        } else if (!std::isfinite(state->_calib_dt_CAMtoIMU->value()(0))) {
          after.expected_output_timestamp_reason = "IMU_TIME_OFFSET_NONFINITE";
        } else {
          after.expected_output_timestamp =
              state->_timestamp + state->_calib_dt_CAMtoIMU->value()(0);
          after.expected_output_timestamp_available =
              std::isfinite(after.expected_output_timestamp);
          after.expected_output_timestamp_reason =
              after.expected_output_timestamp_available
                  ? "NONE"
                  : "EXPECTED_OUTPUT_TIMESTAMP_NONFINITE";
        }
      }
      after_metadata_available = true;
    }
    const std::lock_guard<std::mutex> lock(mutex_);
    if (!active_.load(std::memory_order_acquire) || !current_available_) return;
    if (after_metadata_available) {
      current_.outcome_association_keys.initialized_after_available =
          after.initialized_after_available;
      current_.outcome_association_keys.initialized_after =
          after.initialized_after;
      current_.outcome_association_keys.state_timestamp_after_available =
          after.state_timestamp_after_available;
      current_.outcome_association_keys.state_timestamp_after =
          after.state_timestamp_after;
      current_.outcome_association_keys.state_timestamp_after_reason =
          after.state_timestamp_after_reason;
      current_.outcome_association_keys.expected_output_timestamp_available =
          after.expected_output_timestamp_available;
      current_.outcome_association_keys.expected_output_timestamp =
          after.expected_output_timestamp;
      current_.outcome_association_keys.expected_output_timestamp_reason =
          after.expected_output_timestamp_reason;
      current_.outcome_association_keys.estimator_valid_after_available =
          after.estimator_valid_after_available;
      current_.outcome_association_keys.estimator_valid_after =
          after.estimator_valid_after;
      current_.outcome_association_keys.nonfinite_observed =
          after.nonfinite_observed;
      current_.outcome_association_keys.nonfinite_observed_available =
          after.nonfinite_observed_available;
      current_.outcome_association_keys.nonfinite_observed_reason =
          after.nonfinite_observed_reason;
      current_.outcome_association_keys.callback_incomplete =
          after.callback_incomplete;
      current_.outcome_association_keys.callback_completion_reason =
          std::move(after.callback_completion_reason);
    }
    std::sort(current_.frontend.begin(), current_.frontend.end(),
              [](const ov_core::TrackKLTFrameDiagnostics &left,
                 const ov_core::TrackKLTFrameDiagnostics &right) {
                return left.camera_id < right.camera_id;
              });
    const double timestamp = current_.timestamp;
    const bool accepted_ordinary_full_update =
        current_.update_available &&
        current_.update.baseline_commit_occurred &&
        !current_.update.baseline_accepted_ids.empty();
    const bool updater_timestamp_matches =
        !current_.update_available ||
        same_timestamp(timestamp, current_.update.callback_timestamp);
    if (!updater_timestamp_matches) {
      current_.no_full_visual_update_duration_reason =
          CallbackRecord::DurationReason::kUpdaterTimestampMismatch;
    } else if (!std::isfinite(timestamp)) {
      current_.no_full_visual_update_duration_reason =
          CallbackRecord::DurationReason::kTimestampNonfinite;
    } else if (last_camera_timestamp_available_ &&
               timestamp < last_camera_timestamp_) {
      current_.no_full_visual_update_duration_reason =
          CallbackRecord::DurationReason::kTimestampRegression;
    } else if (accepted_ordinary_full_update) {
      current_.no_full_visual_update_duration_available = true;
      current_.no_full_visual_update_duration = 0.0;
      current_.no_full_visual_update_duration_reason =
          CallbackRecord::DurationReason::kNone;
      last_full_visual_timestamp_available_ = true;
      last_full_visual_timestamp_ = timestamp;
    } else if (last_full_visual_timestamp_available_) {
      current_.no_full_visual_update_duration_available = true;
      current_.no_full_visual_update_duration =
          timestamp - last_full_visual_timestamp_;
      current_.no_full_visual_update_duration_reason =
          CallbackRecord::DurationReason::kNone;
    }
    if (updater_timestamp_matches && std::isfinite(timestamp) &&
        (!last_camera_timestamp_available_ ||
         timestamp >= last_camera_timestamp_)) {
      last_camera_timestamp_available_ = true;
      last_camera_timestamp_ = timestamp;
    }
    if (std::isfinite(timestamp) &&
        (!previous_processed_callback_available_ ||
         timestamp > previous_processed_callback_timestamp_)) {
      previous_processed_callback_available_ = true;
      previous_processed_callback_timestamp_ = timestamp;
    } else {
      previous_processed_callback_available_ = false;
    }
    if (initialized_available) {
      const bool initialized_during_callback =
          current_.initialized_before_available &&
          !current_.initialized_before && estimator_initialized;
      const bool initialized_since_previous_callback =
          previous_initialized_after_available_ &&
          !previous_initialized_after_ && estimator_initialized;
      if (initialized_during_callback || initialized_since_previous_callback) {
        next_callback_initialization_boundary_ = true;
      }
      previous_initialized_after_available_ = true;
      previous_initialized_after_ = estimator_initialized;
    }
    inject_diagnostic_fault(
        TurnSafeDiagnosticFaultStage::kCallbackSerialization);
    const std::string line = SerializeCallback(current_);
    if (WriteLine(line)) ++callbacks_written_;
    current_ = CallbackRecord();
    current_available_ = false;
  } catch (...) {
    DisableForCurrentException();
  }
}

void TurnSafeDiagnostics::ReportComponentFailure(
    TurnSafeCaptureDisableReason reason) noexcept {
  if (reason != TurnSafeCaptureDisableReason::kFrontendCaptureFailure &&
      reason != TurnSafeCaptureDisableReason::kUpdaterCaptureFailure) {
    reason = TurnSafeCaptureDisableReason::kDiagnosticUnknownException;
  }
  Disable(reason);
}

TurnSafeFullTrackAttempt TurnSafeDiagnostics::CaptureAttempt(
    const ov_core::Feature &feature,
    std::uint64_t detached_index) {
    inject_diagnostic_fault(TurnSafeDiagnosticFaultStage::kAttemptCopy);
    TurnSafeFullTrackAttempt output;
    output.detached_index = detached_index;
    output.feature_id = static_cast<std::uint64_t>(feature.featid);
    std::vector<std::size_t> camera_ids;
    camera_ids.reserve(feature.timestamps.size());
    for (const auto &camera : feature.timestamps)
      camera_ids.push_back(camera.first);
    std::sort(camera_ids.begin(), camera_ids.end());
    std::size_t observation_index = 0U;
    for (const std::size_t camera_id : camera_ids) {
      const auto timestamps = feature.timestamps.find(camera_id);
      if (timestamps == feature.timestamps.end())
        throw std::runtime_error("captured camera identity disappeared");
      const auto uv = feature.uvs.find(camera_id);
      const auto uv_norm = feature.uvs_norm.find(camera_id);
      for (std::size_t index = 0U; index < timestamps->second.size(); ++index) {
        TurnSafeObservationValue observation;
        observation.camera_id = camera_id;
        observation.timestamp = timestamps->second[index];
        observation.baseline_observation_index = observation_index++;
        if (uv != feature.uvs.end() && index < uv->second.size() &&
            uv->second[index].size() >= 2) {
          observation.uv_available = true;
          observation.uv = {{static_cast<double>(uv->second[index](0)),
                             static_cast<double>(uv->second[index](1))}};
        }
        if (uv_norm != feature.uvs_norm.end() &&
            index < uv_norm->second.size() && uv_norm->second[index].size() >= 2) {
          observation.uv_normalized_available = true;
          observation.uv_normalized =
              {{static_cast<double>(uv_norm->second[index](0)),
                static_cast<double>(uv_norm->second[index](1))}};
        }
        output.ordered_observations.push_back(std::move(observation));
      }
    }
  return output;
}

void TurnSafeDiagnostics::CapturePriorPrimitives(
    const MSCKFUpdatePriorSnapshot &prior,
    std::vector<TurnSafeFullTrackAttempt> &attempts,
    TurnSafePriorPrimitives &output) {
    inject_diagnostic_fault(TurnSafeDiagnosticFaultStage::kPriorCopy);
    output = TurnSafePriorPrimitives();
    if (!validate_prior_partition(prior)) {
      output.reason = "PRIOR_LAYOUT_OR_FINITE_VALIDATION_FAILED";
      return;
    }
    output.covariance_dimension =
        static_cast<std::size_t>(prior.filter.covariance.rows());
    output.fingerprint = prior_fingerprint(prior);

    std::map<std::string, double> used_timestamps;
    std::map<std::pair<std::string, std::string>,
             std::pair<double, double>> used_pairs;
    for (auto &attempt : attempts) {
      for (auto &observation : attempt.ordered_observations) {
        observation.clone_available =
            find_binding(prior, observation.timestamp) != nullptr;
      }
      const std::vector<CandidateKey> candidates = enumerate_candidates(attempt);
      attempt.valid_clone_pair_count =
          static_cast<std::uint64_t>(candidates.size());
      attempt.attempt_has_any_live_same_camera_clone_pair =
          !candidates.empty();
      for (const CandidateKey &candidate : candidates) {
        used_timestamps[timestamp_key(candidate.source)] = candidate.source;
        used_timestamps[timestamp_key(candidate.target)] = candidate.target;
        used_pairs[{timestamp_key(candidate.source),
                    timestamp_key(candidate.target)}] =
            {candidate.source, candidate.target};
        for (const auto &observation : attempt.ordered_observations) {
          if (observation.camera_id != candidate.camera_id &&
              same_timestamp(observation.timestamp, candidate.target)) {
            attempt.target_time_stereo_available = true;
            break;
          }
        }
      }
    }

    for (const auto &timestamp_entry : used_timestamps) {
      const double timestamp = timestamp_entry.second;
      const auto *binding = find_binding(prior, timestamp);
      if (binding == nullptr) continue;
      const auto *nominal = find_nominal(prior, binding->covariance_id);
      if (nominal == nullptr || nominal->size != 6 ||
          nominal->value.rows() != 7 || nominal->value.cols() != 1 ||
          nominal->fej.rows() != 7 || nominal->fej.cols() != 1 ||
          !nominal->value.allFinite() || !nominal->fej.allFinite()) {
        output.reason = "CLONE_VALUE_LAYOUT_INVALID";
        output.clones.clear();
        output.pair_covariances.clear();
        return;
      }
      TurnSafePriorCloneValue clone;
      clone.timestamp = timestamp;
      clone.covariance_id = binding->covariance_id;
      clone.nominal_pose = capture_matrix(nominal->value);
      clone.fej_pose = capture_matrix(nominal->fej);
      output.clones.push_back(std::move(clone));
    }

    for (const auto &pair_entry : used_pairs) {
      const std::pair<double, double> &pair = pair_entry.second;
      const auto *source = find_binding(prior, pair.first);
      const auto *target = find_binding(prior, pair.second);
      if (source == nullptr || target == nullptr || source->covariance_id < 0 ||
          target->covariance_id < 0 ||
          source->covariance_id + 6 > prior.filter.covariance.rows() ||
          target->covariance_id + 6 > prior.filter.covariance.rows()) {
        output.reason = "CLONE_COVARIANCE_BLOCK_UNAVAILABLE";
        output.clones.clear();
        output.pair_covariances.clear();
        return;
      }
      TurnSafePriorPairCovariance covariance;
      covariance.source_timestamp = pair.first;
      covariance.target_timestamp = pair.second;
      covariance.source_covariance_id = source->covariance_id;
      covariance.target_covariance_id = target->covariance_id;
      covariance.source_source = capture_matrix(prior.filter.covariance.block(
          source->covariance_id, source->covariance_id, 6, 6));
      covariance.target_target = capture_matrix(prior.filter.covariance.block(
          target->covariance_id, target->covariance_id, 6, 6));
      covariance.source_target = capture_matrix(prior.filter.covariance.block(
          source->covariance_id, target->covariance_id, 6, 6));
      covariance.target_source = capture_matrix(prior.filter.covariance.block(
          target->covariance_id, source->covariance_id, 6, 6));
      output.pair_covariances.push_back(std::move(covariance));
    }

    for (const auto &camera : prior.cameras) {
      TurnSafePriorCameraValue copied;
      copied.camera_id = camera.camera_id;
      copied.extrinsic_id = camera.extrinsic_id;
      copied.intrinsic_id = camera.intrinsic_id;
      copied.extrinsic_value = capture_matrix(camera.extrinsic_value);
      copied.extrinsic_fej = capture_matrix(camera.extrinsic_fej);
      copied.intrinsic_value = capture_matrix(camera.intrinsic_value);
      copied.intrinsic_fej = capture_matrix(camera.intrinsic_fej);
      copied.projection_cache = capture_matrix(camera.cache_value);
      copied.width = camera.width;
      copied.height = camera.height;
      switch (camera.model) {
      case MSCKFUpdatePriorCameraModel::kRadtan: copied.model = "RADTAN"; break;
      case MSCKFUpdatePriorCameraModel::kEquidistant: copied.model = "EQUIDISTANT"; break;
      case MSCKFUpdatePriorCameraModel::kUnknown: copied.model = "UNKNOWN"; break;
      }
      output.cameras.push_back(std::move(copied));
    }
    std::sort(output.cameras.begin(), output.cameras.end(),
              [](const TurnSafePriorCameraValue &left,
                 const TurnSafePriorCameraValue &right) {
                return left.camera_id < right.camera_id;
              });
    output.available = true;
    output.reason =
        used_pairs.empty() ? "NO_LIVE_SAME_CAMERA_CLONE_PAIRS" : "NONE";
}

TurnSafeAcuteCertificateResult
TurnSafeDiagnostics::EvaluateAcuteCertificate(
    double translation_ucb, double range_lcb,
    const Eigen::Matrix2d &bearing_covariance) {
  TurnSafeAcuteCertificateResult output;
    if (!std::isfinite(translation_ucb) || !std::isfinite(range_lcb) ||
        translation_ucb < 0.0 || range_lcb <= 0.0) {
      output.status = "INVALID_INPUT";
      return output;
    }
    if (translation_ucb >= range_lcb) {
      output.status = "TRANSLATION_NOT_ACUTE";
      return output;
    }
    if (!bearing_covariance.allFinite()) {
      output.status = "BEARING_COVARIANCE_NONFINITE";
      return output;
    }
    Eigen::SelfAdjointEigenSolver<Eigen::Matrix2d> solver(
        bearing_covariance, Eigen::EigenvaluesOnly);
    if (solver.info() != Eigen::Success || !solver.eigenvalues().allFinite() ||
        solver.eigenvalues()(0) <= 0.0) {
      output.status = "BEARING_COVARIANCE_NUMERICAL_FAILURE";
      return output;
    }
    output.eigenvalues_available = true;
    output.lambda_min = solver.eigenvalues()(0);
    output.lambda_max = solver.eigenvalues()(1);
    output.is_acute = true;
    output.theta_ucb = std::asin(translation_ucb / range_lcb);
    output.theta_available = std::isfinite(output.theta_ucb);
    output.rho_trans = output.theta_ucb / std::sqrt(output.lambda_min);
    output.rho_available = output.theta_available && std::isfinite(output.rho_trans);
    output.status = output.rho_available ? "AVAILABLE" : "NUMERICAL_FAILURE";
  return output;
}

std::string TurnSafeDiagnostics::SerializeCallback(
    const CallbackRecord &record) {
  std::ostringstream output;
  std::uint64_t previous_tracks = 0U, klt_survivors = 0U,
                bounds_survivors = 0U, mask_survivors = 0U,
                ransac_inputs = 0U, ransac_survivors = 0U,
                native_combined_survivors = 0U, database_writes = 0U;
  output << "{\"schema\":\"turnsafe.t0.v1\",\"record_type\":\"callback\""
         << ",\"callback_index\":" << record.callback_index
         << ",\"callback_timestamp_value\":" << double_json(record.timestamp)
         << ",\"callback_timestamp_key\":" << quote(timestamp_key(record.timestamp))
         << ",\"prior_fingerprint\":";
  if (record.update_available && record.update.prior.available)
    output << quote(record.update.prior.fingerprint);
  else
    output << "{\"status\":\"NOT_EXPOSED\",\"reason\":\"UPDATER_PRIOR_NOT_CAPTURED\"}";

  output << ",\"frontend_cameras\":[";
  for (std::size_t index = 0U; index < record.frontend.size(); ++index) {
    const auto &frame = record.frontend[index];
    if (index != 0U) output << ',';
    previous_tracks += frame.previous_track_count;
    klt_survivors += frame.klt_status_survivors;
    bounds_survivors += frame.in_bounds_survivors;
    mask_survivors += frame.mask_survivors;
    ransac_inputs += frame.fmatrix_input_points;
    ransac_survivors += frame.fmatrix_inliers;
    native_combined_survivors += frame.native_combined_track_survivors;
    database_writes += frame.database_observations_written;
    output << "{\"camera_id\":" << frame.camera_id
           << ",\"source_camera_timestamp\":";
    if (frame.source_timestamp_available)
      output << "{\"status\":\"AVAILABLE\",\"value\":"
             << double_json(frame.source_timestamp) << ",\"key\":"
             << quote(timestamp_key(frame.source_timestamp)) << '}';
    else
      output << "{\"status\":\"NOT_APPLICABLE\",\"reason\":\"INITIAL_FRAME\"}";
    output << ",\"frame_timestamp_value\":"
           << double_json(frame.target_timestamp)
           << ",\"frame_timestamp_key\":"
           << quote(timestamp_key(frame.target_timestamp))
           << ",\"previous_tracked_points\":" << frame.previous_track_count
           << ",\"reseed_count\":" << frame.reseed_count
           << ",\"klt_attempted_count\":" << frame.klt_attempted_count
           << ",\"klt_counts_available\":" << (frame.klt_counts_available ? "true" : "false")
           << ",\"klt_status_survivors\":" << frame.klt_status_survivors
           << ",\"klt_status_rejections\":" << frame.klt_status_rejections
           << ",\"out_of_bounds_rejections\":" << frame.out_of_bounds_rejections
           << ",\"in_bounds_survivors\":" << frame.in_bounds_survivors
           << ",\"mask_stage_present\":" << (frame.mask_stage_present ? "true" : "false")
           << ",\"mask_rejections\":" << frame.mask_rejections
           << ",\"mask_survivors\":" << frame.mask_survivors
           << ",\"fmatrix_input_points\":" << frame.fmatrix_input_points
           << ",\"fmatrix_inliers\":" << frame.fmatrix_inliers
           << ",\"fmatrix_rejections\":" << frame.fmatrix_rejections
           << ",\"native_combined_track_survivors\":"
           << frame.native_combined_track_survivors
           << ",\"database_observations_written\":" << frame.database_observations_written
           << ",\"database_tracked_observations_written\":{\"status\":\"NOT_EXPOSED\",\"reason\":\"WRITE_ORIGIN_NOT_EXPOSED_BY_NATIVE_PATH\"}"
           << ",\"database_new_observations_written\":{\"status\":\"NOT_EXPOSED\",\"reason\":\"WRITE_ORIGIN_NOT_EXPOSED_BY_NATIVE_PATH\"}"
           << ",\"reset_too_few_points\":" << (frame.reset_too_few_points ? "true" : "false")
           << ",\"reset_native_reason\":"
           << quote(ov_core::track_klt_native_reason_name(
                  frame.reset_native_reason))
           << ",\"forward_backward_check\":{\"status\":\"NOT_APPLICABLE\",\"reason\":\"NATIVE_KLT_HAS_NO_FORWARD_BACKWARD_CHECK\"}"
           << ",\"klt_error_summary\":" << klt_error_summary_json(frame.surviving_klt_errors)
           << ",\"gyro_magnitude_rad_s\":{\"status\":\"NOT_EXPOSED\",\"reason\":\"CAUSAL_FRAME_INTERVAL_PRIMITIVE_NOT_EXPOSED\"}"
           << ",\"gyro_integrated_rotation_rad\":{\"status\":\"NOT_EXPOSED\",\"reason\":\"CAUSAL_INTEGRAL_NOT_EXPOSED\"}"
           << ",\"blur_metric\":{\"status\":\"NOT_EXPOSED\",\"reason\":\"NOT_EXPOSED_BY_NATIVE_PATH\"}"
           << ",\"exposure\":{\"status\":\"NOT_EXPOSED\",\"reason\":\"NOT_EXPOSED_BY_NATIVE_PATH\"}"
           << ",\"gain\":{\"status\":\"NOT_EXPOSED\",\"reason\":\"NOT_EXPOSED_BY_NATIVE_PATH\"}"
           << ",\"native_accepted_feature_ids\":[";
    for (std::size_t id = 0U; id < frame.native_accepted_feature_ids.size(); ++id) {
      if (id != 0U) output << ',';
      output << frame.native_accepted_feature_ids[id];
    }
    output << ']';
    if (options_.capture_causal_imu_intervals) {
      output << ",\"extensions\":{\"event_extension\":{\"schema\":"
             << quote(options_.event_extension_schema_version)
             << ",\"gyro_interval_id\":"
             << record.causal_imu_interval.gyro_interval_id << "}}";
    }
    output << '}';
  }
  output << ']';

  std::vector<TurnSafeFullTrackAttempt> attempts;
  if (record.update_available) attempts = record.update.attempts;
  std::sort(attempts.begin(), attempts.end(),
            [](const TurnSafeFullTrackAttempt &left,
               const TurnSafeFullTrackAttempt &right) {
              if (left.detached_index != right.detached_index)
                return left.detached_index < right.detached_index;
              return left.feature_id < right.feature_id;
            });
  for (std::size_t index = 1U; index < attempts.size(); ++index) {
    if (attempts[index - 1U].detached_index ==
        attempts[index].detached_index) {
      throw std::runtime_error("duplicate detached attempt identity");
    }
  }
  std::map<std::string, std::uint64_t> outcome_counts;
  std::map<std::string, std::uint64_t> triangulation_outcome_counts;
  std::map<std::string, std::uint64_t> refinement_outcome_counts;
  std::map<std::string, std::uint64_t> schur_outcome_counts;
  std::map<std::string, std::uint64_t> nis_outcome_counts;
  std::uint64_t valid_clone_pairs = 0U,
                attempts_with_valid_clone_pairs = 0U,
                accepted_factors = 0U, accepted_rows = 0U;
  output << ",\"full_track_attempts\":[";
  for (std::size_t index = 0U; index < attempts.size(); ++index) {
    const auto &attempt = attempts[index];
    if (index != 0U) output << ',';
    ++outcome_counts[turnsafe_full_outcome_name(attempt.full_outcome)];
    ++triangulation_outcome_counts[
        initializer_outcome_name(attempt.triangulation)];
    ++refinement_outcome_counts[
        initializer_outcome_name(attempt.refinement)];
    ++schur_outcome_counts[attempt.schur.attempted
                               ? attempt.schur.native_status
                               : "NOT_ATTEMPTED"];
    ++nis_outcome_counts[attempt.full_nis.decision];
    valid_clone_pairs += attempt.valid_clone_pair_count;
    if (attempt.attempt_has_any_live_same_camera_clone_pair)
      ++attempts_with_valid_clone_pairs;
    if (attempt.accepted_full_factor) ++accepted_factors;
    accepted_rows += attempt.accepted_full_row_count;
    output << "{\"detached_index\":" << attempt.detached_index
           << ",\"feature_id\":" << attempt.feature_id
           << ",\"ordered_observation_keys\":[";
    for (std::size_t obs = 0U; obs < attempt.ordered_observations.size(); ++obs) {
      const auto &observation = attempt.ordered_observations[obs];
      if (obs != 0U) output << ',';
      output << "{\"camera_id\":" << observation.camera_id
             << ",\"timestamp_value\":" << double_json(observation.timestamp)
             << ",\"timestamp_key\":" << quote(timestamp_key(observation.timestamp))
             << ",\"feature_id\":" << attempt.feature_id
             << ",\"detached_index\":" << attempt.detached_index
             << ",\"baseline_observation_index\":" << observation.baseline_observation_index
             << ",\"clone_available\":" << (observation.clone_available ? "true" : "false")
             << ",\"uv\":";
      if (observation.uv_available && std::isfinite(observation.uv[0]) &&
          std::isfinite(observation.uv[1])) {
        output << '[' << double_json(observation.uv[0]) << ','
               << double_json(observation.uv[1]) << ']';
      } else if (observation.uv_available) {
        output << "{\"status\":\"NONFINITE\",\"reason\":\"RAW_PIXEL_NONFINITE\"}";
      } else {
        output << "{\"status\":\"NOT_EXPOSED\",\"reason\":\"RAW_PIXEL_NOT_EXPOSED_BY_NATIVE_PATH\"}";
      }
      output << ",\"uv_normalized\":";
      if (observation.uv_normalized_available &&
          std::isfinite(observation.uv_normalized[0]) &&
          std::isfinite(observation.uv_normalized[1])) {
        output << '[' << double_json(observation.uv_normalized[0]) << ','
               << double_json(observation.uv_normalized[1]) << ']';
      } else if (observation.uv_normalized_available) {
        output << "{\"status\":\"NONFINITE\",\"reason\":\"NORMALIZED_PIXEL_NONFINITE\"}";
      } else {
        output << "{\"status\":\"NOT_EXPOSED\",\"reason\":\"NORMALIZED_PIXEL_NOT_EXPOSED_BY_NATIVE_PATH\"}";
      }
      output << '}';
    }
    output << "],\"attempt_has_any_live_same_camera_clone_pair\":"
           << (attempt.attempt_has_any_live_same_camera_clone_pair ? "true" : "false")
           << ",\"attempt_valid_clone_pair_count\":"
           << attempt.valid_clone_pair_count
           << ",\"triangulation\":" << initializer_json(attempt.triangulation)
           << ",\"refinement\":" << initializer_json(attempt.refinement)
           << ",\"schur\":{\"attempted\":" << (attempt.schur.attempted ? "true" : "false")
           << ",\"native_accepted\":" << (attempt.schur.native_accepted ? "true" : "false")
           << ",\"native_status\":" << quote(attempt.schur.native_status)
           << ",\"native_stage\":" << quote(attempt.schur.native_stage)
           << ",\"raw_rows\":"
           << (attempt.schur.raw_rows_available
                   ? std::to_string(attempt.schur.raw_rows)
                   : "{\"status\":\"NOT_EXPOSED\",\"reason\":\"NOT_EXPOSED_BY_NATIVE_PATH\"}")
           << ",\"rows_before_reduction\":"
           << (attempt.schur.raw_rows_available
                   ? std::to_string(attempt.schur.raw_rows)
                   : "{\"status\":\"NOT_EXPOSED\",\"reason\":\"NOT_EXPOSED_BY_NATIVE_PATH\"}")
           << ",\"degrees_of_freedom\":"
           << (attempt.schur.degrees_of_freedom_available
                   ? std::to_string(attempt.schur.degrees_of_freedom)
                   : "{\"status\":\"NOT_EXPOSED\",\"reason\":\"NOT_EXPOSED_BY_NATIVE_PATH\"}")
           << ",\"rows_after_reduction\":"
           << (attempt.schur.reduced_rows_available
                   ? std::to_string(attempt.schur.reduced_rows)
                   : "{\"status\":\"NOT_EXPOSED\",\"reason\":\"NOT_EXPOSED_BY_NATIVE_PATH\"}")
           << ",\"singular_values\":";
    if (attempt.schur.singular_values_available) {
      output << '[' << double_json(attempt.schur.singular_values[0]) << ','
             << double_json(attempt.schur.singular_values[1]) << ','
             << double_json(attempt.schur.singular_values[2]) << ']';
    } else {
      output << "{\"status\":\"NOT_EXPOSED\",\"reason\":\"NOT_EXPOSED_BY_NATIVE_PATH\"}";
    }
    output << ",\"numerical_rank\":"
           << (attempt.schur.numerical_rank_available
                   ? std::to_string(attempt.schur.numerical_rank)
                   : "{\"status\":\"NOT_EXPOSED\",\"reason\":\"NOT_EXPOSED_BY_NATIVE_PATH\"}")
           << ",\"reciprocal_condition\":"
           << numeric_availability(attempt.schur.singular_ratio_available,
                                   attempt.schur.singular_ratio,
                                   "NOT_EXPOSED_BY_NATIVE_PATH")
           << ",\"condition_number\":"
           << numeric_availability(attempt.schur.condition_number_available,
                                   attempt.schur.condition_number,
                                   "NOT_EXPOSED_BY_NATIVE_PATH")
           << ",\"numerical_repairs\":{\"jitter\":"
           << attempt.schur.jitter_count << ",\"clamp\":"
           << attempt.schur.clamp_count << ",\"regularization\":"
           << attempt.schur.regularization_count << ",\"fallback\":"
           << attempt.schur.fallback_count << "}}"
           << ",\"full_nis\":{\"attempted\":" << (attempt.full_nis.attempted ? "true" : "false")
           << ",\"native_stage\":" << quote(attempt.full_nis.native_stage)
           << ",\"lifecycle_accept\":" << (attempt.full_nis.lifecycle_accept ? "true" : "false")
           << ",\"degrees_of_freedom\":"
           << (attempt.full_nis.degrees_of_freedom_available
                   ? std::to_string(attempt.full_nis.degrees_of_freedom)
                   : "{\"status\":\"NOT_EXPOSED\",\"reason\":\"NOT_EXPOSED_BY_NATIVE_PATH\"}")
           << ",\"statistic\":" << numeric_availability(attempt.full_nis.statistic_available, attempt.full_nis.statistic, "NOT_EXPOSED_BY_NATIVE_PATH")
           << ",\"threshold\":" << numeric_availability(attempt.full_nis.threshold_available, attempt.full_nis.threshold, "NOT_EXPOSED_BY_NATIVE_PATH")
           << ",\"decision\":" << quote(attempt.full_nis.decision) << '}'
           << ",\"full_outcome\":" << quote(turnsafe_full_outcome_name(attempt.full_outcome))
           << ",\"full_outcome_mapping\":" << quote(turnsafe_outcome_mapping_name(attempt.full_outcome_mapping))
           << ",\"native_terminal_status\":" << quote(attempt.native_terminal_status)
           << ",\"target_time_stereo_available\":" << (attempt.target_time_stereo_available ? "true" : "false")
           << ",\"accepted_at_native_feature_gate\":"
           << (attempt.accepted_at_native_feature_gate ? "true" : "false")
           << ",\"native_feature_row_count\":"
           << (attempt.native_feature_row_count_available
                   ? std::to_string(attempt.native_feature_row_count)
                   : "{\"status\":\"NOT_EXPOSED\",\"reason\":\"NOT_EXPOSED_BY_NATIVE_PATH\"}")
           << ",\"accepted_full_factor\":" << (attempt.accepted_full_factor ? "true" : "false")
           << ",\"accepted_full_row_count\":" << attempt.accepted_full_row_count
           << ",\"accepted_full_row_status\":"
           << quote(attempt.accepted_full_factor
                        ? "CONTRIBUTED_TO_ACCEPTED_GLOBAL_PROPOSAL"
                        : "NOT_CONTRIBUTED_TO_ACCEPTED_GLOBAL_PROPOSAL")
           << ",\"finalization_result\":" << quote(attempt.finalization_result) << '}';
  }
  output << ']';

  inject_diagnostic_fault(TurnSafeDiagnosticFaultStage::kCandidateGrouping);
  std::vector<CallbackCandidate> callback_candidates;
  for (std::size_t attempt_index = 0U; attempt_index < attempts.size();
       ++attempt_index) {
    const std::vector<CandidateKey> local =
        enumerate_candidates(attempts[attempt_index]);
    for (const CandidateKey &key : local) {
      CallbackCandidate candidate;
      candidate.key = key;
      candidate.attempt_index = attempt_index;
      callback_candidates.push_back(candidate);
    }
  }
  std::sort(callback_candidates.begin(), callback_candidates.end(),
            [](const CallbackCandidate &left,
               const CallbackCandidate &right) {
              return candidate_key_less(left.key, right.key);
            });
  for (std::size_t index = 1U; index < callback_candidates.size(); ++index) {
    const CandidateKey &left = callback_candidates[index - 1U].key;
    const CandidateKey &right = callback_candidates[index].key;
    if (!candidate_key_less(left, right) &&
        !candidate_key_less(right, left)) {
      throw std::runtime_error("duplicate canonical candidate key");
    }
  }

  const auto write_candidate_key = [&output](const CandidateKey &candidate) {
    output << "{\"camera_id\":" << candidate.camera_id
           << ",\"source_timestamp_key\":"
           << quote(timestamp_key(candidate.source))
           << ",\"target_timestamp_key\":"
           << quote(timestamp_key(candidate.target))
           << ",\"feature_id\":" << candidate.feature_id
           << ",\"detached_index\":" << candidate.detached_index
           << ",\"source_observation_ordinal\":"
           << candidate.source_observation_index
           << ",\"target_observation_ordinal\":"
           << candidate.target_observation_index << '}';
  };
  const auto write_observation_key =
      [&output](const TurnSafeObservationValue &observation,
                const TurnSafeFullTrackAttempt &attempt) {
        output << "{\"camera_id\":"
               << observation.camera_id << ",\"timestamp_value\":"
               << double_json(observation.timestamp)
               << ",\"timestamp_key\":"
               << quote(timestamp_key(observation.timestamp))
               << ",\"feature_id\":" << attempt.feature_id
               << ",\"detached_index\":" << attempt.detached_index
               << ",\"observation_ordinal\":"
               << observation.baseline_observation_index << '}';
      };
  const auto write_observation_evidence =
      [&output, &write_observation_key](
          const TurnSafeObservationValue &observation,
          const TurnSafeFullTrackAttempt &attempt) {
        output << "{\"observation_key\":";
        write_observation_key(observation, attempt);
        output << ",\"raw_pixel\":";
        if (observation.uv_available) {
          if (std::isfinite(observation.uv[0]) &&
              std::isfinite(observation.uv[1])) {
            output << '[' << double_json(observation.uv[0]) << ','
                   << double_json(observation.uv[1]) << ']';
          } else {
            output << "{\"status\":\"NONFINITE\",\"reason\":\"RAW_PIXEL_NONFINITE\"}";
          }
        } else {
          output << "{\"status\":\"NOT_EXPOSED\",\"reason\":\"RAW_PIXEL_NOT_EXPOSED_BY_NATIVE_PATH\"}";
        }
        output << ",\"normalized_pixel\":";
        if (observation.uv_normalized_available) {
          if (std::isfinite(observation.uv_normalized[0]) &&
              std::isfinite(observation.uv_normalized[1])) {
            output << '[' << double_json(observation.uv_normalized[0]) << ','
                   << double_json(observation.uv_normalized[1]) << ']';
          } else {
            output << "{\"status\":\"NONFINITE\",\"reason\":\"NORMALIZED_PIXEL_NONFINITE\"}";
          }
        } else {
          output << "{\"status\":\"NOT_EXPOSED\",\"reason\":\"NORMALIZED_PIXEL_NOT_EXPOSED_BY_NATIVE_PATH\"}";
        }
        output << '}';
      };
  const auto find_camera = [&record](std::size_t camera_id)
      -> const TurnSafePriorCameraValue * {
    if (!record.update_available) return nullptr;
    for (const auto &camera : record.update.prior.cameras) {
      if (camera.camera_id == camera_id) return &camera;
    }
    return nullptr;
  };
  const auto camera_identity_available = [](
      const TurnSafePriorCameraValue *camera) {
    return camera != nullptr && camera->intrinsic_id >= 0 &&
           camera->extrinsic_id >= 0 && !camera->model.empty() &&
           camera->model != "UNKNOWN";
  };
  const auto write_camera_identity =
      [&output, &find_camera, &camera_identity_available](std::size_t camera_id) {
        const TurnSafePriorCameraValue *camera = find_camera(camera_id);
        if (!camera_identity_available(camera)) {
          output << "{\"status\":\"NOT_EXPOSED\",\"camera_id\":"
                 << camera_id
                 << ",\"reason\":\"CAMERA_CALIBRATION_NOT_EXPOSED_BY_NATIVE_PATH\"}";
          return;
        }
        output << "{\"status\":\"AVAILABLE\",\"camera_id\":"
               << camera->camera_id << ",\"model\":"
               << quote(camera->model) << ",\"intrinsic_id\":"
               << camera->intrinsic_id << ",\"extrinsic_id\":"
               << camera->extrinsic_id << '}';
      };

  std::map<PairKey, std::vector<std::size_t>, decltype(&pair_key_less)>
      grouped_candidates(&pair_key_less);
  std::set<std::uint64_t> attempts_with_target_stereo;
  std::set<std::tuple<std::size_t, std::string, std::string>>
      groups_with_target_stereo;
  std::uint64_t candidate_pairs_with_target_stereo = 0U;

  output << ",\"shadow_candidates\":[";
  for (std::size_t index = 0U; index < callback_candidates.size(); ++index) {
    const CallbackCandidate &callback_candidate = callback_candidates[index];
    const CandidateKey &candidate = callback_candidate.key;
    const TurnSafeFullTrackAttempt &attempt =
        attempts[callback_candidate.attempt_index];
    const TurnSafeObservationValue &source =
        attempt.ordered_observations.at(candidate.source_observation_index);
    const TurnSafeObservationValue &target =
        attempt.ordered_observations.at(candidate.target_observation_index);
    const TurnSafeObservationValue *stereo = find_target_stereo(
        attempt, candidate,
        options_.resolved_configuration.camera_count);
    const TurnSafePriorCameraValue *source_camera =
        find_camera(source.camera_id);
    const TurnSafePriorCameraValue *target_camera =
        find_camera(target.camera_id);
    const TurnSafePriorCameraValue *stereo_camera =
        stereo == nullptr ? nullptr : find_camera(stereo->camera_id);
    const bool target_stereo_available = stereo != nullptr;
    if (target_stereo_available) {
      ++candidate_pairs_with_target_stereo;
      attempts_with_target_stereo.insert(
          static_cast<std::uint64_t>(callback_candidate.attempt_index));
      groups_with_target_stereo.insert(std::make_tuple(
          candidate.camera_id, timestamp_key(candidate.source),
          timestamp_key(candidate.target)));
    }
    PairKey pair;
    pair.camera_id = candidate.camera_id;
    pair.source = candidate.source;
    pair.target = candidate.target;
    grouped_candidates[pair].push_back(index);

    if (index != 0U) output << ',';
    output << "{\"candidate_key\":";
    write_candidate_key(candidate);
    output << ",\"source_observation_key\":";
    write_observation_key(source, attempt);
    output << ",\"source_observation\":";
    write_observation_evidence(source, attempt);
    output << ",\"target_observation_key\":";
    write_observation_key(target, attempt);
    output << ",\"target_observation\":";
    write_observation_evidence(target, attempt);
    output << ",\"supporting_target_time_stereo_observation_key\":";
    if (stereo != nullptr) write_observation_key(*stereo, attempt);
    else output << "{\"status\":\"NOT_AVAILABLE_FROM_SENSOR\",\"reason\":\"TARGET_STEREO_UNAVAILABLE\"}";
    output << ",\"supporting_target_time_stereo_observation\":";
    if (stereo != nullptr) write_observation_evidence(*stereo, attempt);
    else output << "{\"status\":\"NOT_AVAILABLE_FROM_SENSOR\",\"reason\":\"TARGET_STEREO_UNAVAILABLE\"}";
    output << ",\"camera_calibration\":{\"source\":";
    write_camera_identity(source.camera_id);
    output << ",\"target\":";
    write_camera_identity(target.camera_id);
    output << ",\"stereo\":";
    if (stereo != nullptr) write_camera_identity(stereo->camera_id);
    else output << "{\"status\":\"NOT_APPLICABLE\",\"reason\":\"TARGET_STEREO_UNAVAILABLE\"}";
    const char *stereo_reason = "NONE";
    if (!options_.resolved_configuration.stereo_enabled ||
        !options_.resolved_configuration.stereo_available) {
      stereo_reason = "RUNTIME_STEREO_CONFIGURATION_UNAVAILABLE";
    } else if (stereo == nullptr) {
      stereo_reason = "CONFIGURED_TARGET_STEREO_OBSERVATION_UNAVAILABLE";
    } else if (!source.uv_available) {
      stereo_reason = "SOURCE_RAW_PIXEL_NOT_EXPOSED_BY_NATIVE_PATH";
    } else if (!std::isfinite(source.uv[0]) ||
               !std::isfinite(source.uv[1])) {
      stereo_reason = "SOURCE_RAW_PIXEL_NONFINITE";
    } else if (!target.uv_available) {
      stereo_reason = "TARGET_RAW_PIXEL_NOT_EXPOSED_BY_NATIVE_PATH";
    } else if (!std::isfinite(target.uv[0]) ||
               !std::isfinite(target.uv[1])) {
      stereo_reason = "TARGET_RAW_PIXEL_NONFINITE";
    } else if (!stereo->uv_available) {
      stereo_reason = "TARGET_STEREO_RAW_PIXEL_NOT_EXPOSED_BY_NATIVE_PATH";
    } else if (!std::isfinite(stereo->uv[0]) ||
               !std::isfinite(stereo->uv[1])) {
      stereo_reason = "TARGET_STEREO_RAW_PIXEL_NONFINITE";
    } else if (!camera_identity_available(source_camera)) {
      stereo_reason = "SOURCE_CAMERA_CALIBRATION_NOT_EXPOSED_BY_NATIVE_PATH";
    } else if (!camera_identity_available(target_camera)) {
      stereo_reason = "TARGET_CAMERA_CALIBRATION_NOT_EXPOSED_BY_NATIVE_PATH";
    } else if (!camera_identity_available(stereo_camera)) {
      stereo_reason =
          "TARGET_STEREO_CAMERA_CALIBRATION_NOT_EXPOSED_BY_NATIVE_PATH";
    }
    const bool stereo_geometry_available =
        std::strcmp(stereo_reason, "NONE") == 0;
    const bool stereo_geometry_nonfinite =
        std::strcmp(stereo_reason, "SOURCE_RAW_PIXEL_NONFINITE") == 0 ||
        std::strcmp(stereo_reason, "TARGET_RAW_PIXEL_NONFINITE") == 0 ||
        std::strcmp(stereo_reason,
                    "TARGET_STEREO_RAW_PIXEL_NONFINITE") == 0;
    const char *stereo_geometry_status =
        stereo_geometry_available
            ? "AVAILABLE"
            : (stereo_geometry_nonfinite
                   ? "NONFINITE"
                   : ((!options_.resolved_configuration.stereo_enabled ||
                       !options_.resolved_configuration.stereo_available)
                          ? "UNSUPPORTED_CONFIGURATION"
                          : (stereo == nullptr
                                 ? "NOT_AVAILABLE_FROM_SENSOR"
                                 : "NOT_EXPOSED")));
    output << "},\"full_outcome\":"
           << quote(turnsafe_full_outcome_name(attempt.full_outcome))
           << ",\"target_time_stereo_available\":"
           << (target_stereo_available ? "true" : "false")
           << ",\"target_stereo_rejection_reason\":"
           << quote(stereo_reason)
           << ",\"stereo_geometry_primitives\":{\"status\":"
           << quote(stereo_geometry_status)
           << ",\"reason\":" << quote(stereo_reason) << '}'
           << ",\"range_certificate\":{\"status\":\"NOT_APPLICABLE\",\"typed_result\":\"SHADOW_NOT_COMPUTED_STEREO_RANGE_COVARIANCE_NOT_ESTABLISHED\",\"reason\":\"STEREO_RANGE_COVARIANCE_NOT_CONVENTION_TESTED\"}"
           << ",\"translation_certificate\":{\"status\":\"NOT_APPLICABLE\",\"typed_result\":\"SHADOW_NOT_COMPUTED_RELATIVE_CAMERA_CENTER_JACOBIAN_NOT_CERTIFIED\",\"gamma_P\":2.0,\"component_confidence\":0.99865,\"reason\":\"CAMERA_CENTER_COVARIANCE_JACOBIAN_NOT_CONVENTION_TESTED\"}"
           << ",\"bearing_covariance\":{\"status\":\"NOT_APPLICABLE\",\"typed_result\":\"SHADOW_NOT_COMPUTED_PIXEL_TO_BEARING_JACOBIAN_NOT_CERTIFIED\",\"reason\":\"CAMRADTAN_LOCAL_INVERSE_JACOBIAN_NOT_CONVENTION_TESTED\"}"
           << ",\"target_bearing\":{\"status\":\"NOT_EXPOSED\",\"reason\":\"PIXEL_TO_BEARING_JACOBIAN_NOT_CERTIFIED\"}"
           << ",\"acute_regime\":{\"status\":\"NOT_APPLICABLE\",\"typed_result\":\"SHADOW_NOT_COMPUTED_CERTIFICATE_PREREQUISITE_UNAVAILABLE\",\"reason\":\"CERTIFICATE_PREREQUISITE_UNAVAILABLE\"}"
           << ",\"rho_trans\":{\"status\":\"NOT_APPLICABLE\",\"reason\":\"CERTIFICATE_PREREQUISITE_UNAVAILABLE\"}"
           << ",\"rho_threshold\":{\"status\":\"NOT_APPLICABLE\",\"reason\":\"THRESHOLD_SET_NOT_FROZEN\"}"
           << ",\"static_quality_status\":\"NOT_APPLICABLE_THRESHOLD_SET_NOT_FROZEN\""
           << ",\"eligible_before_group\":false,\"terminal_reason\":";
    if (attempt.full_outcome_mapping != TurnSafeOutcomeMapping::kExact)
      output << "\"FULL_OUTCOME_UNMAPPED\"";
    else if (!turnsafe_shadow_eligible_outcome(attempt.full_outcome))
      output << "\"FULL_OUTCOME_INELIGIBLE\"";
    else if (!target_stereo_available)
      output << "\"TARGET_STEREO_UNAVAILABLE\"";
    else
      output << "\"RANGE_LCB_UNAVAILABLE\"";
    output << '}';
  }
  output << ']';

  std::uint64_t groups_n1 = 0U, groups_n2 = 0U, groups_n3 = 0U,
                groups_n_ge_4 = 0U;
  output << ",\"shadow_groups\":[";
  std::size_t group_index = 0U;
  for (const auto &group : grouped_candidates) {
    if (group_index++ != 0U) output << ',';
    std::set<std::pair<std::uint64_t, std::uint64_t>> distinct_features;
    for (std::size_t candidate_index : group.second) {
      const CandidateKey &key = callback_candidates[candidate_index].key;
      distinct_features.insert({key.feature_id, key.detached_index});
    }
    const std::size_t cardinality = distinct_features.size();
    if (cardinality < 2U) ++groups_n1;
    else if (cardinality == 2U) ++groups_n2;
    else if (cardinality == 3U) ++groups_n3;
    else ++groups_n_ge_4;
    const bool small_group = cardinality == 2U || cardinality == 3U;
    output << "{\"pair_key\":{\"camera_id\":"
           << group.first.camera_id << ",\"source_timestamp_key\":"
           << quote(timestamp_key(group.first.source))
           << ",\"target_timestamp_key\":"
           << quote(timestamp_key(group.first.target)) << '}'
           << ",\"candidate_keys\":[";
    for (std::size_t member = 0U; member < group.second.size(); ++member) {
      if (member != 0U) output << ',';
      write_candidate_key(callback_candidates[group.second[member]].key);
    }
    output << "],\"eligible_candidate_keys\":[]"
           << ",\"raw_candidate_count\":" << group.second.size()
           << ",\"pair_group_feature_count\":" << cardinality
           << ",\"eligible_feature_count\":0"
           << ",\"shadow_only_small_group\":"
           << (small_group ? "true" : "false")
           << ",\"group_size_status\":"
           << quote(cardinality < 2U
                        ? "INSUFFICIENT_FOR_PAIR_GROUP"
                        : (small_group ? "SHADOW_ONLY_SMALL_GROUP"
                                       : "PRODUCTION_MINIMUM_MET"))
           << ",\"contains_target_stereo_candidate\":"
           << (groups_with_target_stereo.count(std::make_tuple(
                   group.first.camera_id, timestamp_key(group.first.source),
                   timestamp_key(group.first.target))) != 0U
                   ? "true" : "false")
           << ",\"target_bearing_spatial_metrics\":{\"status\":\"NOT_APPLICABLE\",\"reason\":\"THRESHOLD_SET_NOT_FROZEN\"}"
           << ",\"rotation_stack_singular_values\":{\"status\":\"NOT_APPLICABLE\",\"reason\":\"CERTIFICATE_PREREQUISITE_UNAVAILABLE\"}"
           << ",\"rotation_stack_rank\":{\"status\":\"NOT_APPLICABLE\",\"reason\":\"CERTIFICATE_PREREQUISITE_UNAVAILABLE\"}"
           << ",\"consensus\":{\"status\":\"NOT_APPLICABLE\",\"typed_result\":\"SHADOW_NOT_COMPUTED_THRESHOLD_SET_NOT_FROZEN\",\"reason\":\"THRESHOLD_SET_NOT_FROZEN\"}"
           << ",\"eligible_before_selection\":false"
           << ",\"predicted_rotation_angle\":{\"status\":\"NOT_APPLICABLE\",\"reason\":\"CERTIFICATE_PREREQUISITE_UNAVAILABLE\"}"
           << ",\"predicted_information\":{\"status\":\"NOT_APPLICABLE\",\"reason\":\"CERTIFICATE_PREREQUISITE_UNAVAILABLE\"}"
           << ",\"score\":{\"status\":\"NOT_APPLICABLE\",\"reason\":\"INELIGIBLE_GROUP\"}"
           << ",\"selection_role\":\"INELIGIBLE\""
           << ",\"foregone_reason\":\"NOT_APPLICABLE\""
           << ",\"winner_shadow_nis\":{\"status\":\"NOT_APPLICABLE\",\"reason\":\"NOT_FROZEN_WINNER\"}"
           << ",\"post_nis_count_rank_status\":{\"status\":\"NOT_APPLICABLE\",\"reason\":\"NOT_FROZEN_WINNER\"}}";
  }
  output << ']';

  output << ",\"prior_primitives\":";
  if (record.update_available && record.update.prior.available) {
    const auto &prior = record.update.prior;
    output << "{\"status\":\"AVAILABLE\",\"reason\":" << quote(prior.reason)
           << ",\"covariance_dimension\":" << prior.covariance_dimension
           << ",\"clones\":[";
    for (std::size_t index = 0U; index < prior.clones.size(); ++index) {
      if (index != 0U) output << ',';
      const auto &clone = prior.clones[index];
      output << "{\"timestamp_value\":" << double_json(clone.timestamp)
             << ",\"timestamp_key\":" << quote(timestamp_key(clone.timestamp))
             << ",\"covariance_id\":" << clone.covariance_id
             << ",\"nominal_pose\":" << matrix_json(clone.nominal_pose)
             << ",\"fej_pose\":" << matrix_json(clone.fej_pose) << '}';
    }
    output << "],\"pair_covariances\":[";
    for (std::size_t index = 0U; index < prior.pair_covariances.size(); ++index) {
      if (index != 0U) output << ',';
      const auto &covariance = prior.pair_covariances[index];
      output << "{\"source_timestamp_key\":" << quote(timestamp_key(covariance.source_timestamp))
             << ",\"target_timestamp_key\":" << quote(timestamp_key(covariance.target_timestamp))
             << ",\"source_covariance_id\":" << covariance.source_covariance_id
             << ",\"target_covariance_id\":" << covariance.target_covariance_id
             << ",\"P_ss\":" << matrix_json(covariance.source_source)
             << ",\"P_tt\":" << matrix_json(covariance.target_target)
             << ",\"P_st\":" << matrix_json(covariance.source_target)
             << ",\"P_ts\":" << matrix_json(covariance.target_source) << '}';
    }
    output << "],\"cameras\":[";
    for (std::size_t index = 0U; index < prior.cameras.size(); ++index) {
      if (index != 0U) output << ',';
      const auto &camera = prior.cameras[index];
      output << "{\"camera_id\":" << camera.camera_id
             << ",\"model\":" << quote(camera.model)
             << ",\"extrinsic_id\":" << camera.extrinsic_id
             << ",\"intrinsic_id\":" << camera.intrinsic_id
             << ",\"width\":" << camera.width << ",\"height\":" << camera.height
             << ",\"extrinsic_value\":" << matrix_json(camera.extrinsic_value)
             << ",\"extrinsic_fej\":" << matrix_json(camera.extrinsic_fej)
             << ",\"intrinsic_value\":" << matrix_json(camera.intrinsic_value)
             << ",\"intrinsic_fej\":" << matrix_json(camera.intrinsic_fej)
             << ",\"projection_cache\":" << matrix_json(camera.projection_cache) << '}';
    }
    output << "]}";
  } else {
    output << "{\"status\":\"NOT_EXPOSED\",\"reason\":\"UPDATER_PRIOR_NOT_CAPTURED\"}";
  }

  output << ",\"baseline_decision\":{\"native_status\":"
         << quote(record.update_available ? record.update.native_terminal_status : "UPDATER_NOT_REACHED")
         << ",\"native_subreason\":"
         << quote(record.update_available ? record.update.native_terminal_subreason : "UPDATER_NOT_REACHED")
         << ",\"accepted_full_feature_ids\":[";
  if (record.update_available) {
    for (std::size_t index = 0U; index < record.update.baseline_accepted_ids.size(); ++index) {
      if (index != 0U) output << ',';
      output << record.update.baseline_accepted_ids[index];
    }
  }
  output << "]"
         << ",\"proposal_sufficient_statistics\":{\"gamma\":"
         << (record.update_available ? numeric_availability(record.update.proposal_gamma_available, record.update.proposal_gamma, "GAMMA_UNAVAILABLE") : "{\"status\":\"NOT_APPLICABLE\",\"reason\":\"UPDATER_NOT_REACHED\"}")
         << ",\"precompression_rows\":"
         << (record.update_available && record.update.precompression_rows_available
                 ? std::to_string(record.update.precompression_rows)
                 : "{\"status\":\"NOT_EXPOSED\",\"reason\":\"NOT_EXPOSED_BY_NATIVE_PATH\"}")
         << ",\"compressed_rows\":"
         << (record.update_available && record.update.compressed_rows_available
                 ? std::to_string(record.update.compressed_rows)
                 : "{\"status\":\"NOT_EXPOSED\",\"reason\":\"NOT_EXPOSED_BY_NATIVE_PATH\"}")
         << '}'
         << ",\"proposal_attempted\":" << (record.update_available && record.update.proposal_attempted ? "true" : "false")
         << ",\"proposal_accepted\":" << (record.update_available && record.update.proposal_accepted ? "true" : "false")
         << ",\"baseline_commit_occurred\":" << (record.update_available && record.update.baseline_commit_occurred ? "true" : "false")
         << ",\"no_full_visual_update_duration\":";
  if (record.no_full_visual_update_duration_available) {
    output << numeric_availability(
        true, record.no_full_visual_update_duration, "NONE");
  } else {
    const char *reason = "NO_PRIOR_ACCEPTED_FULL_UPDATE";
    switch (record.no_full_visual_update_duration_reason) {
    case CallbackRecord::DurationReason::kNone: reason = "NONE"; break;
    case CallbackRecord::DurationReason::kNoPriorAcceptedFullUpdate:
      reason = "NO_PRIOR_ACCEPTED_FULL_UPDATE";
      break;
    case CallbackRecord::DurationReason::kTimestampNonfinite:
      reason = "CAMERA_TIMESTAMP_NONFINITE";
      break;
    case CallbackRecord::DurationReason::kTimestampRegression:
      reason = "CAMERA_TIMESTAMP_REGRESSION";
      break;
    case CallbackRecord::DurationReason::kUpdaterTimestampMismatch:
      reason = "UPDATER_CALLBACK_TIMESTAMP_MISMATCH";
      break;
    }
    output << "{\"status\":\"NOT_AVAILABLE\",\"reason\":"
           << quote(reason) << '}';
  }
  output
         << ",\"mean_commit_count\":" << (record.update_available ? record.update.mean_commit_count : 0U)
         << ",\"covariance_commit_count\":" << (record.update_available ? record.update.covariance_commit_count : 0U)
         << ",\"terminal_finalization_count\":" << (record.update_available ? record.update.feature_finalization_count : 0U)
         << ",\"nominal_state_fingerprint\":{\"status\":\"NOT_EXPOSED\",\"reason\":\"FILE_BOUND_PARITY_USED\"}"
         << ",\"covariance_fingerprint\":{\"status\":\"NOT_EXPOSED\",\"reason\":\"FILE_BOUND_PARITY_USED\"}}";

  const auto write_outcome_counts = [&output](
      const std::map<std::string, std::uint64_t> &counts) {
    output << '{';
    std::size_t index = 0U;
    for (const auto &count : counts) {
      if (index++ != 0U) output << ',';
      output << quote(count.first) << ':' << count.second;
    }
    output << '}';
  };

  output << ",\"funnel\":{\"camera_frames\":" << record.frontend.size()
         << ",\"previous_tracked_points\":" << previous_tracks
         << ",\"klt_status_survivors\":" << klt_survivors
         << ",\"in_bounds_survivors\":" << bounds_survivors
         << ",\"mask_survivors\":" << mask_survivors
         << ",\"fmatrix_input_points\":" << ransac_inputs
         << ",\"fmatrix_survivors\":" << ransac_survivors
         << ",\"native_combined_track_survivors\":"
         << native_combined_survivors
         << ",\"database_observations_written\":" << database_writes
         << ",\"terminal_attempt_count\":" << attempts.size()
         << ",\"tracks_with_valid_clone_pairs\":"
         << attempts_with_valid_clone_pairs
         << ",\"attempt_valid_clone_pair_count_total\":"
         << valid_clone_pairs
         << ",\"full_outcome_counts_by_code\":";
  write_outcome_counts(outcome_counts);
  output << ",\"triangulation_outcome_counts_by_code\":";
  write_outcome_counts(triangulation_outcome_counts);
  output << ",\"refinement_outcome_counts_by_code\":";
  write_outcome_counts(refinement_outcome_counts);
  output << ",\"schur_outcome_counts_by_code\":";
  write_outcome_counts(schur_outcome_counts);
  output << ",\"full_nis_outcome_counts_by_code\":";
  write_outcome_counts(nis_outcome_counts);
  output << ",\"accepted_full_factors\":" << accepted_factors
         << ",\"accepted_full_rows\":" << accepted_rows
         << ",\"shadow_pair_candidates\":"
         << callback_candidates.size()
         << ",\"candidate_pairs_with_target_stereo\":"
         << candidate_pairs_with_target_stereo
         << ",\"attempts_with_at_least_one_target_stereo_pair\":"
         << attempts_with_target_stereo.size()
         << ",\"groups_with_at_least_one_target_stereo_pair\":"
         << groups_with_target_stereo.size()
         << ",\"candidates_with_calibrated_range_lcb\":0"
         << ",\"candidates_with_calibrated_translation_ucb\":0"
         << ",\"candidates_translation_acute\":0"
         << ",\"candidates_passing_rho_trans\":0"
         << ",\"groups_n_lt_2\":" << groups_n1
         << ",\"groups_n_2\":" << groups_n2
         << ",\"groups_n_3\":" << groups_n3
         << ",\"groups_n_ge_4\":" << groups_n_ge_4
         << ",\"groups_passing_rank\":0,\"groups_passing_consensus\":0"
         << ",\"eligible_groups_before_selection\":0,\"winner_count\":0"
         << ",\"foregone_eligible_groups\":0,\"foregone_eligible_features\":0"
         << ",\"winner_shadow_factors_passing_nis\":0"
         << ",\"winner_shadow_rows_passing_nis\":0}";

  output << ",\"shadow_summary\":{\"eligible_group_count\":0"
         << ",\"frozen_winner\":{\"status\":\"NOT_APPLICABLE\",\"typed_result\":\"SHADOW_NOT_COMPUTED_THRESHOLD_SET_NOT_FROZEN\"}"
         << ",\"runner_up\":{\"status\":\"NOT_APPLICABLE\",\"reason\":\"NO_ELIGIBLE_GROUP\"}"
         << ",\"foregone_eligible_groups\":0,\"foregone_eligible_features\":0"
         << ",\"no_runner_up_gate_shopping\":true,\"translation_covariance_scale\":2.0}"
         << ",\"completeness\":{\"status\":\"PARTIAL_SHADOW_BY_CONTRACT\""
         << ",\"typed_reasons\":[\"SHADOW_NOT_COMPUTED_STEREO_RANGE_COVARIANCE_NOT_ESTABLISHED\",\"SHADOW_NOT_COMPUTED_RELATIVE_CAMERA_CENTER_JACOBIAN_NOT_CERTIFIED\",\"SHADOW_NOT_COMPUTED_PIXEL_TO_BEARING_JACOBIAN_NOT_CERTIFIED\",\"SHADOW_NOT_COMPUTED_THRESHOLD_SET_NOT_FROZEN\"]}";
  if (EventExtensionRequested()) {
    inject_diagnostic_fault(TurnSafeDiagnosticFaultStage::kExtensionSerialization);
    output << ",\"extensions\":{\"event_extension\":{\"schema\":"
           << quote(options_.event_extension_schema_version)
           << ",\"record_version\":1";
    if (options_.capture_causal_imu_intervals) {
      output << ",\"causal_imu_interval\":";
      write_causal_interval(output, record.causal_imu_interval,
                            record.callback_index);
    }
    if (options_.capture_outcome_association_keys) {
      output << ",\"outcome_association_keys\":";
      write_outcome_association_keys(
          output, options_, record.callback_index, record.timestamp,
          record.outcome_association_keys, record.update_available,
          record.update, record.no_full_visual_update_duration_available,
          record.no_full_visual_update_duration);
    }
    if (options_.capture_group_bearing_provenance) {
      inject_diagnostic_fault(TurnSafeDiagnosticFaultStage::kGroupProvenance);
      const bool prior_available =
          record.update_available && record.update.prior.available;
      output << ",\"group_bearing_provenance\":{\"status\":"
             << quote(prior_available ? "AVAILABLE" : "NOT_AVAILABLE")
             << ",\"reason\":"
             << quote(prior_available
                          ? "NONE"
                          : (record.update_available &&
                                     !record.update.prior.reason.empty()
                                 ? record.update.prior.reason
                                 : "UPDATER_PRIOR_NOT_CAPTURED"))
             << ",\"schema_version\":\"turnsafe.t0.event_extension.v1\""
             << ",\"canonical_ordering_version\":\"turnsafe.event_extension.order.v1\""
             << ",\"shared_field_encoding\":\"one_group_record_plus_member_array.v1\""
             << ",\"groups\":[";
      const auto find_clone = [&record](double timestamp)
          -> const TurnSafePriorCloneValue * {
        if (!record.update_available) return nullptr;
        for (const auto &clone : record.update.prior.clones) {
          if (same_timestamp(clone.timestamp, timestamp)) return &clone;
        }
        return nullptr;
      };
      std::size_t emitted_groups = 0U;
      for (const auto &group : grouped_candidates) {
        if (!prior_available) break;
        std::vector<std::size_t> members;
        std::set<std::pair<std::uint64_t, std::uint64_t>> identities;
        for (const std::size_t callback_candidate_index : group.second) {
          const CallbackCandidate &candidate_record =
              callback_candidates.at(callback_candidate_index);
          const TurnSafeFullTrackAttempt &attempt =
              attempts.at(candidate_record.attempt_index);
          const CandidateKey &candidate = candidate_record.key;
          if (attempt.full_outcome_mapping != TurnSafeOutcomeMapping::kExact ||
              !turnsafe_shadow_eligible_outcome(attempt.full_outcome) ||
              find_target_stereo(
                  attempt, candidate,
                  options_.resolved_configuration.camera_count) == nullptr) {
            continue;
          }
          const auto identity =
              std::make_pair(candidate.feature_id, candidate.detached_index);
          if (!identities.insert(identity).second) {
            throw std::runtime_error(
                "duplicate canonical group member identity");
          }
          members.push_back(callback_candidate_index);
        }
        if (members.size() < 2U) continue;
        if (emitted_groups++ != 0U) output << ',';
        const TurnSafePriorCameraValue *camera = find_camera(group.first.camera_id);
        const TurnSafePriorCloneValue *source_clone =
            find_clone(group.first.source);
        const TurnSafePriorCloneValue *target_clone =
            find_clone(group.first.target);
        const CallbackCandidate &first_candidate_record =
            callback_candidates.at(members.front());
        const TurnSafeFullTrackAttempt &first_attempt =
            attempts.at(first_candidate_record.attempt_index);
        const TurnSafeObservationValue *first_stereo = find_target_stereo(
            first_attempt, first_candidate_record.key,
            options_.resolved_configuration.camera_count);
        const TurnSafePriorCameraValue *shared_stereo_camera =
            first_stereo == nullptr ? nullptr
                                    : find_camera(first_stereo->camera_id);
        output << "{\"group_key\":{\"camera_id\":"
               << group.first.camera_id
               << ",\"source_timestamp_key\":"
               << quote(timestamp_key(group.first.source))
               << ",\"target_timestamp_key\":"
               << quote(timestamp_key(group.first.target)) << '}'
               << ",\"member_count\":" << members.size()
               << ",\"member_key_list\":[";
        for (std::size_t member = 0U; member < members.size(); ++member) {
          if (member != 0U) output << ',';
          write_candidate_key(callback_candidates.at(members[member]).key);
        }
        output << "],\"camera\":{\"camera_id\":" << group.first.camera_id;
        if (camera == nullptr || !camera_provenance_valid(*camera)) {
          output << ",\"status\":\"NOT_AVAILABLE\",\"reason\":\"CAMERA_CALIBRATION_UNAVAILABLE\"}";
        } else {
          output << ",\"status\":\"AVAILABLE\",\"reason\":\"NONE\",\"model\":"
                 << quote(camera->model) << ",\"image_width\":"
                 << camera->width << ",\"image_height\":" << camera->height
                 << ",\"intrinsics_distortion_hash\":"
                 << quote(canonical_matrix_hash(camera->intrinsic_value))
                 << ",\"camera_extrinsic_hash\":"
                 << quote(canonical_matrix_hash(camera->extrinsic_value))
                 << ",\"hash_algorithm\":\"fnv1a64_binary64_be.v1\"}";
        }
        output << ",\"target_stereo_camera\":{";
        if (shared_stereo_camera == nullptr ||
            !camera_provenance_valid(*shared_stereo_camera)) {
          output << "\"status\":\"NOT_AVAILABLE\",\"reason\":\"TARGET_STEREO_CAMERA_CALIBRATION_UNAVAILABLE\"}";
        } else {
          output << "\"status\":\"AVAILABLE\",\"reason\":\"NONE\",\"camera_id\":"
                 << shared_stereo_camera->camera_id
                 << ",\"model\":" << quote(shared_stereo_camera->model)
                 << ",\"image_width\":" << shared_stereo_camera->width
                 << ",\"image_height\":" << shared_stereo_camera->height
                 << ",\"intrinsics_distortion_hash\":"
                 << quote(canonical_matrix_hash(
                        shared_stereo_camera->intrinsic_value))
                 << ",\"camera_extrinsic_hash\":"
                 << quote(canonical_matrix_hash(
                        shared_stereo_camera->extrinsic_value))
                 << ",\"hash_algorithm\":\"fnv1a64_binary64_be.v1\"}";
        }
        output << ",\"bearing_provenance\":\"direct_native_normalized_unit_ray.v1\""
               << ",\"source_clone_timestamp_value\":"
               << double_json(group.first.source)
               << ",\"target_clone_timestamp_value\":"
               << double_json(group.first.target)
               << ",\"source_current_R_GtoI\":";
        write_rotation(output,
                       source_clone == nullptr ? nullptr
                                               : &source_clone->nominal_pose,
                       "SOURCE_CURRENT_CLONE_ROTATION_UNAVAILABLE");
        output << ",\"source_fej_R_GtoI\":";
        write_rotation(output,
                       source_clone == nullptr ? nullptr
                                               : &source_clone->fej_pose,
                       "SOURCE_FEJ_CLONE_ROTATION_UNAVAILABLE");
        output << ",\"target_current_R_GtoI\":";
        write_rotation(output,
                       target_clone == nullptr ? nullptr
                                               : &target_clone->nominal_pose,
                       "TARGET_CURRENT_CLONE_ROTATION_UNAVAILABLE");
        output << ",\"target_fej_R_GtoI\":";
        write_rotation(output,
                       target_clone == nullptr ? nullptr
                                               : &target_clone->fej_pose,
                       "TARGET_FEJ_CLONE_ROTATION_UNAVAILABLE");
        output << ",\"fixed_R_ItoC\":";
        write_rotation(output,
                       camera == nullptr ? nullptr
                                         : &camera->extrinsic_value,
                       "CAMERA_EXTRINSIC_ROTATION_UNAVAILABLE");
        output << ",\"supported_configuration\":{\"value\":"
               << (options_.resolved_configuration.supported ? "true" : "false")
               << ",\"reasons\":[";
        for (std::size_t reason = 0U;
             reason < options_.resolved_configuration.unsupported_reasons.size();
             ++reason) {
          if (reason != 0U) output << ',';
          output << quote(
              options_.resolved_configuration.unsupported_reasons[reason]);
        }
        output << "]},\"members\":[";
        for (std::size_t member = 0U; member < members.size(); ++member) {
          if (member != 0U) output << ',';
          const CallbackCandidate &candidate_record =
              callback_candidates.at(members[member]);
          const CandidateKey &candidate = candidate_record.key;
          const TurnSafeFullTrackAttempt &attempt =
              attempts.at(candidate_record.attempt_index);
          const TurnSafeObservationValue &source =
              attempt.ordered_observations.at(
                  candidate.source_observation_index);
          const TurnSafeObservationValue &target =
              attempt.ordered_observations.at(
                  candidate.target_observation_index);
          const TurnSafeObservationValue *stereo = find_target_stereo(
              attempt, candidate,
              options_.resolved_configuration.camera_count);
          const TurnSafePriorCameraValue *stereo_camera =
              stereo == nullptr ? nullptr : find_camera(stereo->camera_id);
          if (stereo != nullptr && first_stereo != nullptr &&
              stereo->camera_id != first_stereo->camera_id) {
            throw std::runtime_error(
                "group target-stereo camera identity is not shared");
          }
          const bool finite = source.uv_available &&
              std::isfinite(source.uv[0]) && std::isfinite(source.uv[1]) &&
              source.uv_normalized_available &&
              std::isfinite(source.uv_normalized[0]) &&
              std::isfinite(source.uv_normalized[1]) &&
              target.uv_available && std::isfinite(target.uv[0]) &&
              std::isfinite(target.uv[1]) &&
              target.uv_normalized_available &&
              std::isfinite(target.uv_normalized[0]) &&
              std::isfinite(target.uv_normalized[1]) && stereo != nullptr &&
              stereo->uv_available && std::isfinite(stereo->uv[0]) &&
              std::isfinite(stereo->uv[1]);
          output << "{\"member_key\":";
          write_candidate_key(candidate);
          output << ",\"feature_id\":" << candidate.feature_id
                 << ",\"detached_index\":" << candidate.detached_index
                 << ",\"source_observation_key\":";
          write_observation_key(source, attempt);
          output << ",\"target_observation_key\":";
          write_observation_key(target, attempt);
          output << ",\"target_stereo_observation_key\":";
          if (stereo != nullptr) write_observation_key(*stereo, attempt);
          else output << "{\"status\":\"NOT_AVAILABLE\",\"reason\":\"TARGET_STEREO_UNAVAILABLE\"}";
          output << ",\"source_raw_pixel\":";
          write_pixel_value(output, source.uv_available, source.uv,
                            "SOURCE_RAW_PIXEL_UNAVAILABLE",
                            "SOURCE_RAW_PIXEL_NONFINITE");
          output << ",\"target_raw_pixel\":";
          write_pixel_value(output, target.uv_available, target.uv,
                            "TARGET_RAW_PIXEL_UNAVAILABLE",
                            "TARGET_RAW_PIXEL_NONFINITE");
          output << ",\"source_normalized_coordinates\":";
          write_pixel_value(output, source.uv_normalized_available,
                            source.uv_normalized,
                            "SOURCE_NORMALIZED_UNAVAILABLE",
                            "SOURCE_NORMALIZED_NONFINITE");
          output << ",\"target_normalized_coordinates\":";
          write_pixel_value(output, target.uv_normalized_available,
                            target.uv_normalized,
                            "TARGET_NORMALIZED_UNAVAILABLE",
                            "TARGET_NORMALIZED_NONFINITE");
          output << ",\"source_unit_bearing\":";
          write_unit_bearing(output, source, "SOURCE_BEARING_UNAVAILABLE",
                             "SOURCE_BEARING_NONFINITE");
          output << ",\"target_unit_bearing\":";
          write_unit_bearing(output, target, "TARGET_BEARING_UNAVAILABLE",
                             "TARGET_BEARING_NONFINITE");
          output << ",\"target_stereo_raw_pixel\":";
          if (stereo == nullptr)
            output << "{\"status\":\"NOT_AVAILABLE\",\"reason\":\"TARGET_STEREO_UNAVAILABLE\"}";
          else
            write_pixel_value(output, stereo->uv_available, stereo->uv,
                              "TARGET_STEREO_RAW_PIXEL_UNAVAILABLE",
                              "TARGET_STEREO_RAW_PIXEL_NONFINITE");
          output << ",\"source_image_cell\":";
          write_image_cell(output, source, camera);
          output << ",\"target_image_cell\":";
          write_image_cell(output, target, camera);
          output << ",\"target_stereo_image_cell\":";
          if (stereo == nullptr)
            output << "{\"status\":\"NOT_AVAILABLE\",\"reason\":\"TARGET_STEREO_UNAVAILABLE\"}";
          else
            write_image_cell(output, *stereo, stereo_camera);
          output << ",\"native_source_status\":{\"status\":\"AVAILABLE\",\"reason\":\"TYPED_T1_SOURCE_OUTCOME\",\"full_outcome\":"
                 << quote(turnsafe_full_outcome_name(attempt.full_outcome))
                 << "},\"finite_validation\":{\"status\":"
                 << quote(finite ? "AVAILABLE" : "NOT_AVAILABLE")
                 << ",\"reason\":"
                 << quote(finite ? "NONE" : "MEMBER_PRIMITIVE_MISSING_OR_NONFINITE")
                 << "}}";
        }
        output << "]}";
      }
      output << "],\"group_count\":" << emitted_groups << '}';
    }
    output << "}}";
  }
  output << '}';
  return output.str();
}

bool TurnSafeDiagnostics::Finalize() noexcept {
  bool renamed = false;
  try {
    const std::lock_guard<std::mutex> lock(mutex_);
    if (finalized_) {
      return active_.load(std::memory_order_acquire) &&
             failure_reason_.load(std::memory_order_acquire) ==
                 static_cast<std::uint8_t>(
                     TurnSafeCaptureDisableReason::kNone);
    }
    finalized_ = true;
    if (current_available_ && active_.load(std::memory_order_acquire)) {
      Disable(TurnSafeCaptureDisableReason::kUnterminatedCallbackEnvelope);
    }
    bool success = active_.load(std::memory_order_acquire) &&
                   stream_ != nullptr;
    if (stream_ != nullptr) {
      if (success) {
        try {
          inject_diagnostic_fault(
              TurnSafeDiagnosticFaultStage::kSinkFinalFlush);
          if (std::fflush(stream_) != 0 ||
              ::fsync(::fileno(stream_)) != 0) {
            success = false;
            Disable(TurnSafeCaptureDisableReason::
                        kOutputFinalFlushOrFsyncFailed);
          }
        } catch (...) {
          success = false;
          DisableForCurrentException();
        }
      }
      inject_diagnostic_fault(TurnSafeDiagnosticFaultStage::kSinkClose);
      if (std::fclose(stream_) != 0) {
        success = false;
        Disable(TurnSafeCaptureDisableReason::kOutputCloseFailed);
      }
      stream_ = nullptr;
    }
    if (success)
      inject_diagnostic_fault(TurnSafeDiagnosticFaultStage::kSinkRename);
    if (success && ::rename(temporary_path_.c_str(),
                            options_.output_path.c_str()) != 0) {
      success = false;
      Disable(TurnSafeCaptureDisableReason::kOutputAtomicRenameFailed);
    } else if (success) {
      renamed = true;
    }
    if (success) {
      inject_diagnostic_fault(
          TurnSafeDiagnosticFaultStage::kSinkDirectoryFsync);
      const std::size_t slash = options_.output_path.find_last_of('/');
      const std::string parent =
          slash == std::string::npos ? "." : options_.output_path.substr(0, slash);
      const int directory = ::open(parent.c_str(), O_RDONLY | O_DIRECTORY | O_CLOEXEC);
      if (directory < 0 || ::fsync(directory) != 0) {
        success = false;
        Disable(TurnSafeCaptureDisableReason::kOutputDirectoryFsyncFailed);
      }
      if (directory >= 0) ::close(directory);
    }
    if (!success) {
      if (renamed) ::unlink(options_.output_path.c_str());
      else if (!temporary_path_.empty()) ::unlink(temporary_path_.c_str());
    }
    active_.store(success, std::memory_order_release);
    return success;
  } catch (const std::bad_alloc &) {
    if (stream_ != nullptr) std::fclose(stream_);
    stream_ = nullptr;
    if (renamed) ::unlink(options_.output_path.c_str());
    else if (!temporary_path_.empty()) ::unlink(temporary_path_.c_str());
    Disable(TurnSafeCaptureDisableReason::kDiagnosticBadAlloc);
    return false;
  } catch (const std::exception &) {
    if (stream_ != nullptr) std::fclose(stream_);
    stream_ = nullptr;
    if (renamed) ::unlink(options_.output_path.c_str());
    else if (!temporary_path_.empty()) ::unlink(temporary_path_.c_str());
    Disable(TurnSafeCaptureDisableReason::kDiagnosticStdException);
    return false;
  } catch (...) {
    if (stream_ != nullptr) std::fclose(stream_);
    stream_ = nullptr;
    if (renamed) ::unlink(options_.output_path.c_str());
    else if (!temporary_path_.empty()) ::unlink(temporary_path_.c_str());
    Disable(TurnSafeCaptureDisableReason::kDiagnosticUnknownException);
    return false;
  }
}

} // namespace ov_msckf
