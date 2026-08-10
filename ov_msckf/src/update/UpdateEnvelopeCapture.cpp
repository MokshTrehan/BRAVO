/*
 * SchurVIO-Lite research-only update-envelope capture (schema 2).
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include "UpdateEnvelopeCapture.h"

#include "CP2Canonical.h"
#include "feat/Feature.h"
#include "state/State.h"
#include "types/LandmarkRepresentation.h"
#include "types/Type.h"
#include "utils/colors.h"
#include "utils/print.h"

#include <algorithm>
#include <array>
#include <cerrno>
#include <chrono>
#include <cmath>
#include <cstring>
#include <fcntl.h>
#include <fstream>
#include <limits>
#include <stdexcept>
#include <sys/stat.h>
#include <unordered_map>
#include <unordered_set>
#include <unistd.h>

#ifndef SCHURVIO_SOURCE_COMMIT
#define SCHURVIO_SOURCE_COMMIT "unknown"
#endif

namespace ov_msckf {
namespace {

constexpr std::uint64_t kSchemaVersion = 2U;
constexpr std::uint64_t kBinary64Scalar = 1U;
constexpr std::uint64_t kBigEndian = 1U;
constexpr std::uint64_t kTrailerSentinel =
    std::numeric_limits<std::uint64_t>::max();
constexpr char kDomain[] = "schurvio_update_envelope_v2";
constexpr std::array<std::uint8_t, 16> kMagic{{
    'S', 'C', 'V', 'I', 'O', 'U', 'P', 'D',
    'A', 'T', 'E', 'S', '0', '0', '0', '2'}};

std::array<std::uint8_t, 8> encode_u64(std::uint64_t value) noexcept {
  std::array<std::uint8_t, 8> bytes{{0}};
  for (std::size_t i = 0; i < bytes.size(); ++i) {
    bytes[i] = static_cast<std::uint8_t>(value >> (56U - 8U * i));
  }
  return bytes;
}

bool lowercase_hex(const std::string &value, std::size_t size) noexcept {
  if (value.size() != size) {
    return false;
  }
  for (const char character : value) {
    if (!((character >= '0' && character <= '9') ||
          (character >= 'a' && character <= 'f'))) {
      return false;
    }
  }
  return true;
}

bool safe_identity(const std::string &value) noexcept {
  if (value.empty() || value.size() > 128U) {
    return false;
  }
  for (const char character : value) {
    if (!((character >= 'a' && character <= 'z') ||
          (character >= 'A' && character <= 'Z') ||
          (character >= '0' && character <= '9') || character == '_' ||
          character == '-' || character == '.')) {
      return false;
    }
  }
  return true;
}

std::string sha256_file(const std::string &path) {
  std::ifstream input(path, std::ios::in | std::ios::binary);
  if (!input.is_open()) {
    throw std::runtime_error("schema-2 capture cannot open config for hashing");
  }
  CP2Sha256 digest;
  std::array<char, 65536> buffer{{0}};
  while (input) {
    input.read(buffer.data(), static_cast<std::streamsize>(buffer.size()));
    const std::streamsize count = input.gcount();
    if (count > 0) {
      digest.Update(buffer.data(), static_cast<std::size_t>(count));
    }
  }
  if (!input.eof()) {
    throw std::runtime_error("schema-2 capture config hash read failed");
  }
  return digest.HexDigest();
}

void append_bool(CP2CanonicalBuffer &buffer, bool value) {
  buffer.AppendU64(value ? 1U : 0U);
}

bool finite_nonnegative(double value) noexcept {
  return std::isfinite(value) && value >= 0.0;
}

bool scaled_equal(double left, double right) noexcept {
  const double scale = std::max(1.0, std::max(std::abs(left), std::abs(right)));
  return std::abs(left - right) <= 16.0 *
             std::numeric_limits<double>::epsilon() * scale;
}

using CaptureClock = std::chrono::steady_clock;

std::uint64_t elapsed_ns(const CaptureClock::time_point &start,
                         const CaptureClock::time_point &end) noexcept {
  const auto elapsed =
      std::chrono::duration_cast<std::chrono::nanoseconds>(end - start)
          .count();
  return elapsed > 0 ? static_cast<std::uint64_t>(elapsed) : 0U;
}

bool checked_add_u64(std::uint64_t left, std::uint64_t right,
                     std::uint64_t &result) noexcept {
  if (right > std::numeric_limits<std::uint64_t>::max() - left) {
    return false;
  }
  result = left + right;
  return true;
}

void patch_u64(std::vector<std::uint8_t> &bytes, std::size_t offset,
               std::uint64_t value) {
  if (offset > bytes.size() || bytes.size() - offset < 8U) {
    throw std::out_of_range("schema-2 fixed-width patch is outside payload");
  }
  const auto encoded = encode_u64(value);
  std::copy(encoded.begin(), encoded.end(), bytes.begin() + offset);
}

std::uint64_t camera_model_code(MSCKFUpdatePriorCameraModel model) noexcept {
  switch (model) {
  case MSCKFUpdatePriorCameraModel::kRadtan:
    return 1U;
  case MSCKFUpdatePriorCameraModel::kEquidistant:
    return 2U;
  case MSCKFUpdatePriorCameraModel::kUnknown:
    return 0U;
  }
  return 0U;
}

struct CapturedObservation {
  std::uint64_t camera_id = 0U;
  double timestamp = 0.0;
  double pixel_x = 0.0;
  double pixel_y = 0.0;
  double normalized_x = 0.0;
  double normalized_y = 0.0;
};

enum class TrackLifecycle : std::uint64_t {
  kCandidate = 0U,
  kPrefilterRejected = 1U,
  kTriangulationRejected = 2U,
  kRefinementRejected = 3U,
  kRawAvailable = 4U,
  kReductionRejected = 5U,
  kGateRejected = 6U,
  kGateAccepted = 7U,
  kAccumulated = 8U,
};

struct CapturedTrack {
  std::uint64_t ordinal = 0U;
  std::uint64_t feature_id = 0U;
  UpdateEnvelopeCandidateReason reason =
      UpdateEnvelopeCandidateReason::kUnknown;
  TrackLifecycle lifecycle = TrackLifecycle::kCandidate;
  std::uint64_t raw_observation_count = 0U;
  std::uint64_t cleaned_observation_count = 0U;
  bool prefilter_recorded = false;
  bool prefilter_accepted = false;
  std::uint64_t prefilter_duration_ns = 0U;
  bool time_range_available = false;
  double first_timestamp = 0.0;
  double last_timestamp = 0.0;
  double track_age_seconds = 0.0;
  std::vector<CapturedObservation> observations;
  bool parallax_2d_available = false;
  double parallax_2d_rad = 0.0;
  bool maximum_image_motion_available = false;
  double maximum_image_motion_px = 0.0;
  bool last_frame_displacement_available = false;
  double last_frame_displacement_px = 0.0;
  bool final_normalized_location_available = false;
  double final_normalized_x = 0.0;
  double final_normalized_y = 0.0;
  bool causal_motion_available = false;
  std::uint64_t causal_motion_kind = 0U;
  double causal_translation_m = 0.0;
  double causal_rotation_rad = 0.0;

  bool geometry_recorded = false;
  bool triangulation_attempted = false;
  bool triangulation_succeeded = false;
  bool refinement_attempted = false;
  bool refinement_succeeded = false;
  bool geometry_valid = false;
  std::uint64_t geometry_duration_ns = 0U;
  Eigen::Vector3d p_FinG = Eigen::Vector3d::Zero();
  bool depth_available = false;
  double minimum_depth = 0.0;
  bool parallax_3d_available = false;
  double parallax_3d_rad = 0.0;

  bool raw_available = false;
  std::uint64_t jacobian_duration_ns = 0U;
  Eigen::MatrixXd H_x;
  Eigen::MatrixXd H_f;
  Eigen::VectorXd residual;
  std::vector<UpdateEnvelopeLayoutBlock> layout;

  bool reduction_recorded = false;
  std::uint64_t reducer = 0U;
  std::uint64_t reduction_status = 0U;
  std::uint64_t reduction_stage = 0U;
  bool singular_values_available = false;
  Eigen::Vector3d singular_values = Eigen::Vector3d::Zero();
  std::int64_t numerical_rank = -1;
  bool singular_ratio_available = false;
  double singular_ratio = 0.0;
  Eigen::MatrixXd A_f;
  Eigen::VectorXd b_f;
  std::uint64_t reduction_duration_ns = 0U;

  bool gate_recorded = false;
  std::uint64_t gate_stage = 0U;
  std::int64_t gate_dof = 0;
  bool nis_available = false;
  double nis = 0.0;
  bool gate_threshold_available = false;
  double gate_threshold = 0.0;
  bool evidence_decision_available = false;
  bool evidence_accept = false;
  bool lifecycle_accept = false;
  std::uint64_t gating_duration_ns = 0U;
  bool accepted_for_global = false;
  std::uint64_t global_row_start = 0U;
  std::uint64_t global_row_count = 0U;
  std::uint64_t accumulation_duration_ns = 0U;
};

struct CapturedSystem {
  bool available = false;
  std::uint64_t duration_ns = 0U;
  std::vector<UpdateEnvelopeLayoutBlock> layout;
  Eigen::MatrixXd H;
  Eigen::VectorXd residual;
  Eigen::MatrixXd R;
};

struct CapturedEnvelope {
  std::uint64_t update_id = 0U;
  double camera_timestamp = 0.0;
  UpdateEnvelopeTerminalStatus terminal_status =
      UpdateEnvelopeTerminalStatus::kActive;
  std::uint64_t terminal_reason = 0U;
  MSCKFUpdatePriorSnapshot prior;
  std::vector<CapturedTrack> tracks;
  std::unordered_map<std::uint64_t, std::size_t> track_by_id;
  std::vector<std::uint64_t> accepted_ids;
  CapturedSystem selected;
  CapturedSystem compressed;
  bool posterior_recorded = false;
  std::uint64_t preview_status = 0U;
  std::uint64_t preview_stage = 0U;
  bool global_gate_applied = false;
  bool global_nis_available = false;
  double global_nis = 0.0;
  bool global_gate_accepted = false;
  Eigen::VectorXd dx;
  Eigen::MatrixXd P_plus;
  std::uint64_t preview_duration_ns = 0U;
  std::uint64_t mean_commit_count = 0U;
  std::uint64_t covariance_commit_count = 0U;
  std::uint64_t feature_finalization_count = 0U;
  std::uint64_t commit_duration_ns = 0U;
  std::uint64_t record_construction_duration_ns = 0U;
  UpdateEnvelopeCallbackCosts callback_costs;
};

struct EncodedEnvelopePayload {
  std::vector<std::uint8_t> bytes;
  std::size_t serialization_duration_offset = 0U;
  std::size_t encoded_bytes_offset = 0U;
};

std::vector<UpdateEnvelopeLayoutBlock> capture_layout(
    const std::vector<std::shared_ptr<ov_type::Type>> &order,
    Eigen::Index expected_columns) {
  if (expected_columns < 0) {
    throw std::invalid_argument("schema-2 layout has negative width");
  }
  std::vector<UpdateEnvelopeLayoutBlock> layout;
  layout.reserve(order.size());
  std::uint64_t local = 0U;
  for (const auto &variable : order) {
    if (!variable || variable->id() < 0 || variable->size() <= 0) {
      throw std::invalid_argument("schema-2 layout has invalid variable");
    }
    const std::uint64_t size = static_cast<std::uint64_t>(variable->size());
    if (size > std::numeric_limits<std::uint64_t>::max() - local) {
      throw std::overflow_error("schema-2 layout width overflows u64");
    }
    layout.push_back({local, static_cast<std::uint64_t>(variable->id()), size});
    local += size;
  }
  if (local != static_cast<std::uint64_t>(expected_columns)) {
    throw std::invalid_argument("schema-2 layout does not cover matrix");
  }
  return layout;
}

std::uint64_t observation_count(const ov_core::Feature &feature) {
  std::uint64_t result = 0U;
  for (const auto &camera : feature.timestamps) {
    const std::uint64_t count =
        static_cast<std::uint64_t>(camera.second.size());
    if (count > std::numeric_limits<std::uint64_t>::max() - result) {
      throw std::overflow_error("schema-2 observation count overflows u64");
    }
    result += count;
  }
  return result;
}

void capture_prefactor_values(const ov_core::Feature &feature,
                              CapturedTrack &track) {
  track.observations.clear();
  track.cleaned_observation_count = 0U;
  track.time_range_available = false;
  std::unordered_map<std::uint64_t, std::vector<Eigen::Vector3d>>
      bearings_by_camera;
  std::unordered_map<std::uint64_t, std::vector<Eigen::Vector2d>>
      pixels_by_camera;
  double newest_timestamp = -std::numeric_limits<double>::infinity();
  double last_displacement = 0.0;
  bool last_displacement_available = false;
  for (const auto &camera_times : feature.timestamps) {
    const auto pixels_it = feature.uvs.find(camera_times.first);
    const auto normalized_it = feature.uvs_norm.find(camera_times.first);
    if (pixels_it == feature.uvs.end() ||
        normalized_it == feature.uvs_norm.end() ||
        pixels_it->second.size() != camera_times.second.size() ||
        normalized_it->second.size() != camera_times.second.size()) {
      throw std::invalid_argument("schema-2 feature observation maps disagree");
    }
    if (pixels_it->second.size() >= 2U) {
      const Eigen::VectorXf &left = pixels_it->second[pixels_it->second.size() - 2U];
      const Eigen::VectorXf &right = pixels_it->second.back();
      if (left.rows() < 2 || right.rows() < 2) {
        throw std::invalid_argument("schema-2 pixel observation is not 2-D");
      }
      last_displacement = std::max(
          last_displacement,
          (right.head<2>().cast<double>() - left.head<2>().cast<double>()).norm());
      last_displacement_available = true;
    }
    for (std::size_t index = 0; index < camera_times.second.size(); ++index) {
      const Eigen::VectorXf &pixel = pixels_it->second[index];
      const Eigen::VectorXf &normalized = normalized_it->second[index];
      if (pixel.rows() < 2 || normalized.rows() < 2) {
        throw std::invalid_argument("schema-2 observation is not 2-D");
      }
      CapturedObservation observation;
      observation.camera_id =
          static_cast<std::uint64_t>(camera_times.first);
      observation.timestamp = camera_times.second[index];
      observation.pixel_x = static_cast<double>(pixel(0));
      observation.pixel_y = static_cast<double>(pixel(1));
      observation.normalized_x = static_cast<double>(normalized(0));
      observation.normalized_y = static_cast<double>(normalized(1));
      if (!std::isfinite(observation.timestamp) ||
          !std::isfinite(observation.pixel_x) ||
          !std::isfinite(observation.pixel_y) ||
          !std::isfinite(observation.normalized_x) ||
          !std::isfinite(observation.normalized_y)) {
        throw std::invalid_argument("schema-2 observation is nonfinite");
      }
      track.observations.push_back(observation);
      pixels_by_camera[observation.camera_id].emplace_back(
          observation.pixel_x, observation.pixel_y);
      Eigen::Vector3d bearing(observation.normalized_x,
                              observation.normalized_y, 1.0);
      if (!(bearing.norm() > std::numeric_limits<double>::min())) {
        throw std::invalid_argument("schema-2 normalized bearing is degenerate");
      }
      bearings_by_camera[observation.camera_id].push_back(
          bearing.normalized());
      if (!track.time_range_available) {
        track.first_timestamp = observation.timestamp;
        track.last_timestamp = observation.timestamp;
        track.time_range_available = true;
      } else {
        track.first_timestamp = std::min(track.first_timestamp,
                                         observation.timestamp);
        track.last_timestamp = std::max(track.last_timestamp,
                                        observation.timestamp);
      }
      if (observation.timestamp > newest_timestamp) {
        newest_timestamp = observation.timestamp;
        track.final_normalized_x = observation.normalized_x;
        track.final_normalized_y = observation.normalized_y;
        track.final_normalized_location_available = true;
      }
    }
  }
  track.cleaned_observation_count =
      static_cast<std::uint64_t>(track.observations.size());
  if (track.time_range_available) {
    track.track_age_seconds = track.last_timestamp - track.first_timestamp;
    if (!finite_nonnegative(track.track_age_seconds)) {
      throw std::invalid_argument("schema-2 feature age is invalid");
    }
  }
  track.parallax_2d_rad = 0.0;
  track.parallax_2d_available = false;
  for (const auto &camera_bearings : bearings_by_camera) {
    const auto &bearings = camera_bearings.second;
    track.parallax_2d_available =
        track.parallax_2d_available || bearings.size() >= 2U;
    for (std::size_t left = 0; left < bearings.size(); ++left) {
      for (std::size_t right = left + 1U; right < bearings.size(); ++right) {
        const double cosine = std::max(
            -1.0, std::min(1.0, bearings[left].dot(bearings[right])));
        track.parallax_2d_rad =
            std::max(track.parallax_2d_rad, std::acos(cosine));
      }
    }
  }
  track.maximum_image_motion_px = 0.0;
  track.maximum_image_motion_available = false;
  for (const auto &camera_pixels : pixels_by_camera) {
    const auto &pixels = camera_pixels.second;
    track.maximum_image_motion_available =
        track.maximum_image_motion_available || pixels.size() >= 2U;
    for (std::size_t left = 0; left < pixels.size(); ++left) {
      for (std::size_t right = left + 1U; right < pixels.size(); ++right) {
        track.maximum_image_motion_px = std::max(
            track.maximum_image_motion_px,
            (pixels[left] - pixels[right]).norm());
      }
    }
  }
  track.last_frame_displacement_available = last_displacement_available;
  track.last_frame_displacement_px = last_displacement;
}

void capture_causal_motion(const std::shared_ptr<State> &state,
                           CapturedTrack &track) {
  track.causal_motion_available = false;
  track.causal_motion_kind = 0U;
  if (!state || !track.time_range_available) {
    return;
  }
  const auto first = state->_clones_IMU.find(track.first_timestamp);
  const auto last = state->_clones_IMU.find(track.last_timestamp);
  if (first == state->_clones_IMU.end() || last == state->_clones_IMU.end() ||
      !first->second || !last->second) {
    return;
  }
  const Eigen::Vector3d translation =
      last->second->pos() - first->second->pos();
  const Eigen::Matrix3d relative =
      last->second->Rot() * first->second->Rot().transpose();
  const double cosine = std::max(
      -1.0, std::min(1.0, 0.5 * (relative.trace() - 1.0)));
  track.causal_translation_m = translation.norm();
  track.causal_rotation_rad = std::acos(cosine);
  if (finite_nonnegative(track.causal_translation_m) &&
      finite_nonnegative(track.causal_rotation_rad)) {
    track.causal_motion_available = true;
    // Frozen-prior clone displacement, not a new IMU integration.
    track.causal_motion_kind = 1U;
  }
}

void append_layout(CP2CanonicalBuffer &output,
                   const std::vector<UpdateEnvelopeLayoutBlock> &layout) {
  output.AppendU64(static_cast<std::uint64_t>(layout.size()));
  for (const UpdateEnvelopeLayoutBlock &block : layout) {
    output.AppendU64(block.local_column);
    output.AppendU64(block.covariance_column);
    output.AppendU64(block.size);
  }
}

void append_system(CP2CanonicalBuffer &output, UpdateEnvelopeStage stage,
                   const CapturedSystem &system) {
  output.AppendU64(static_cast<std::uint64_t>(stage));
  append_bool(output, system.available);
  output.AppendU64(system.duration_ns);
  append_layout(output, system.layout);
  output.AppendMatrix(system.H);
  output.AppendVector(system.residual);
  output.AppendMatrix(system.R);
}

bool validate_system(const CapturedSystem &system,
                     Eigen::Index state_dimension) noexcept {
  if (!system.available) {
    return system.layout.empty() && system.H.size() == 0 &&
           system.residual.size() == 0 && system.R.size() == 0;
  }
  if (system.H.rows() <= 0 || system.H.cols() <= 0 ||
      system.residual.rows() != system.H.rows() ||
      system.residual.cols() != 1 || system.R.rows() != system.H.rows() ||
      system.R.cols() != system.H.rows() || !system.H.allFinite() ||
      !system.residual.allFinite() || !system.R.allFinite()) {
    return false;
  }
  std::uint64_t expected = 0U;
  const std::uint64_t state_size =
      static_cast<std::uint64_t>(state_dimension);
  for (const auto &block : system.layout) {
    if (block.local_column != expected || block.size == 0U ||
        block.size > state_size ||
        block.covariance_column > state_size - block.size) {
      return false;
    }
    expected += block.size;
  }
  return expected == static_cast<std::uint64_t>(system.H.cols());
}

} // namespace

const char *update_envelope_capture_source_commit() noexcept {
  return SCHURVIO_SOURCE_COMMIT;
}

class UpdateEnvelopeCaptureWriter::Impl {
public:
  explicit Impl(const UpdaterOptions &options) noexcept : options_(options) {
    if (!options_.capture_update_envelopes_v2) {
      return;
    }
    try {
      const std::string commit = update_envelope_capture_source_commit();
      if (!lowercase_hex(commit, 40U) ||
          !safe_identity(options_.update_envelope_run_id) ||
          !safe_identity(options_.update_envelope_sequence_id)) {
        throw std::invalid_argument("schema-2 capture identity is invalid");
      }
      const std::string config_sha256 =
          sha256_file(options_.update_envelope_capture_config_path);
      if (!lowercase_hex(config_sha256, 64U)) {
        throw std::invalid_argument("schema-2 config digest is invalid");
      }
      fd_ = ::open(options_.update_envelope_capture_path.c_str(),
                   O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC,
                   S_IRUSR | S_IWUSR | S_IRGRP | S_IWGRP);
      if (fd_ < 0) {
        throw std::runtime_error("schema-2 output create-new open failed");
      }
      CP2CanonicalBuffer header;
      header.AppendU64(kSchemaVersion);
      header.AppendU64(kBinary64Scalar);
      header.AppendU64(kBigEndian);
      header.AppendUtf8(commit);
      header.AppendUtf8(config_sha256);
      header.AppendUtf8(options_.update_envelope_run_id);
      header.AppendUtf8(options_.update_envelope_sequence_id);
      header.AppendUtf8(kDomain);
      const auto header_size =
          encode_u64(static_cast<std::uint64_t>(header.size()));
      CP2Sha256 digest;
      digest.Update(header.bytes());
      const auto checksum = digest.Digest();
      if (!write_with_digest(kMagic.data(), kMagic.size()) ||
          !write_with_digest(header_size.data(), header_size.size()) ||
          !write_with_digest(header.bytes().data(), header.bytes().size()) ||
          !write_with_digest(checksum.data(), checksum.size())) {
        throw std::runtime_error("schema-2 header write failed");
      }
      active_ = true;
    } catch (const std::exception &exception) {
      disable(exception.what());
    } catch (...) {
      disable("schema-2 initialization failed");
    }
  }

  ~Impl() noexcept { close_file(); }

  bool active() const noexcept { return active_; }
  bool failed() const noexcept { return failed_; }
  bool callback_active() const noexcept { return static_cast<bool>(current_); }
  std::uint64_t envelope_count() const noexcept { return envelope_count_; }
  std::uint64_t callback_record_overhead_ns() const noexcept {
    return current_ ? current_->record_construction_duration_ns : 0U;
  }

  void add_external_record_overhead(std::uint64_t duration_ns) noexcept {
    if (!active_ || !current_) {
      return;
    }
    try {
      add_record_overhead(duration_ns);
    } catch (const std::exception &exception) {
      disable(exception.what());
    } catch (...) {
      disable("schema-2 external record-overhead accounting failed");
    }
  }

  void begin(const std::shared_ptr<State> &state,
             double camera_timestamp) noexcept {
    if (!active_) {
      return;
    }
    const CaptureClock::time_point capture_start = CaptureClock::now();
    try {
      if (current_ || !state || !std::isfinite(camera_timestamp) ||
          envelope_count_ == std::numeric_limits<std::uint64_t>::max()) {
        throw std::logic_error("schema-2 callback begin invariant failed");
      }
      std::unique_ptr<CapturedEnvelope> envelope(new CapturedEnvelope());
      envelope->update_id = envelope_count_;
      envelope->camera_timestamp = camera_timestamp;
      envelope->prior = UpdaterMSCKFPreview::CapturePrior(state);
      current_state_ = state;
      current_ = std::move(envelope);
      add_record_overhead(
          elapsed_ns(capture_start, CaptureClock::now()));
    } catch (const std::exception &exception) {
      disable(exception.what());
    } catch (...) {
      disable("schema-2 callback begin failed");
    }
  }

  void refresh_prior(const std::shared_ptr<State> &state) noexcept {
    mutate("schema-2 prior refresh failed", [&]() {
      if (!state) {
        throw std::invalid_argument("schema-2 prior state is null");
      }
      current_->prior = UpdaterMSCKFPreview::CapturePrior(state);
      current_state_ = state;
    });
  }

  void set_candidates(
      const std::vector<std::shared_ptr<ov_core::Feature>> &features,
      const std::vector<UpdateEnvelopeCandidateReason> &reasons) noexcept {
    mutate("schema-2 candidate freeze failed", [&]() {
      if (!current_->tracks.empty() || !current_->track_by_id.empty() ||
          (!reasons.empty() && reasons.size() != features.size())) {
        throw std::logic_error("schema-2 candidate population is invalid");
      }
      current_->tracks.reserve(features.size());
      for (std::size_t index = 0; index < features.size(); ++index) {
        if (!features[index]) {
          throw std::invalid_argument("schema-2 candidate is null");
        }
        CapturedTrack track;
        track.ordinal = static_cast<std::uint64_t>(index);
        track.feature_id = static_cast<std::uint64_t>(features[index]->featid);
        track.reason = reasons.empty()
                           ? UpdateEnvelopeCandidateReason::kUnknown
                           : reasons[index];
        track.reducer =
            options_.landmark_elimination ==
                    UpdaterOptions::LandmarkElimination::SCHUR
                ? 2U
                : 1U;
        track.raw_observation_count = observation_count(*features[index]);
        if (!current_->track_by_id.emplace(track.feature_id, index).second) {
          throw std::invalid_argument("schema-2 duplicate candidate ID");
        }
        current_->tracks.push_back(std::move(track));
      }
    });
  }

  void record_prefilter(const ov_core::Feature &feature, bool accepted,
                        std::uint64_t duration_ns) noexcept {
    mutate("schema-2 prefilter record failed", [&]() {
      CapturedTrack &track = find_track(feature.featid);
      if (track.prefilter_recorded) {
        throw std::logic_error("schema-2 prefilter recorded twice");
      }
      capture_prefactor_values(feature, track);
      capture_causal_motion(current_state_, track);
      track.prefilter_recorded = true;
      track.prefilter_accepted = accepted;
      track.prefilter_duration_ns = duration_ns;
      if (!accepted) {
        track.lifecycle = TrackLifecycle::kPrefilterRejected;
      }
    });
  }

  void record_geometry(const std::shared_ptr<State> &state,
                       const ov_core::Feature &feature,
                       bool triangulation_attempted,
                       bool triangulation_succeeded,
                       bool refinement_attempted,
                       bool refinement_succeeded,
                       std::uint64_t duration_ns) noexcept {
    mutate("schema-2 geometry record failed", [&]() {
      CapturedTrack &track = find_track(feature.featid);
      if (track.geometry_recorded) {
        throw std::logic_error("schema-2 geometry recorded twice");
      }
      track.geometry_recorded = true;
      track.triangulation_attempted = triangulation_attempted;
      track.triangulation_succeeded = triangulation_succeeded;
      track.refinement_attempted = refinement_attempted;
      track.refinement_succeeded = refinement_succeeded;
      track.geometry_duration_ns = duration_ns;
      if (!triangulation_succeeded) {
        track.lifecycle = TrackLifecycle::kTriangulationRejected;
        return;
      }
      if (!refinement_succeeded) {
        track.lifecycle = TrackLifecycle::kRefinementRejected;
        return;
      }
      if (!state || !feature.p_FinG.allFinite()) {
        return;
      }
      track.p_FinG = feature.p_FinG;
      std::vector<Eigen::Vector3d> rays;
      double minimum_depth = std::numeric_limits<double>::infinity();
      bool valid = true;
      for (const auto &camera_times : feature.timestamps) {
        const auto calibration = state->_calib_IMUtoCAM.find(camera_times.first);
        if (calibration == state->_calib_IMUtoCAM.end() ||
            !calibration->second) {
          valid = false;
          break;
        }
        for (const double timestamp : camera_times.second) {
          const auto clone = state->_clones_IMU.find(timestamp);
          if (clone == state->_clones_IMU.end() || !clone->second) {
            valid = false;
            break;
          }
          const Eigen::Matrix3d R_GtoC =
              calibration->second->Rot() * clone->second->Rot();
          const Eigen::Vector3d p_FinC =
              R_GtoC * (feature.p_FinG - clone->second->pos()) +
              calibration->second->pos();
          const Eigen::Vector3d p_CinG =
              clone->second->pos() -
              R_GtoC.transpose() * calibration->second->pos();
          const Eigen::Vector3d ray = feature.p_FinG - p_CinG;
          minimum_depth = std::min(minimum_depth, p_FinC.z());
          if (!p_FinC.allFinite() || !(p_FinC.z() > 0.0) ||
              !ray.allFinite() ||
              !(ray.norm() > std::numeric_limits<double>::min())) {
            valid = false;
          } else {
            rays.push_back(ray.normalized());
          }
        }
      }
      track.geometry_valid = valid && !rays.empty();
      if (track.geometry_valid) {
        track.depth_available = true;
        track.minimum_depth = minimum_depth;
        track.parallax_3d_available = rays.size() >= 2U;
        track.parallax_3d_rad = 0.0;
        for (std::size_t left = 0; left < rays.size(); ++left) {
          for (std::size_t right = left + 1U; right < rays.size(); ++right) {
            const double cosine = std::max(
                -1.0, std::min(1.0, rays[left].dot(rays[right])));
            track.parallax_3d_rad =
                std::max(track.parallax_3d_rad, std::acos(cosine));
          }
        }
      }
    });
  }

  void record_raw(
      std::uint64_t feature_id,
      const std::vector<std::shared_ptr<ov_type::Type>> &order,
      const Eigen::MatrixXd &H_x, const Eigen::MatrixXd &H_f,
      const Eigen::VectorXd &residual, std::uint64_t duration_ns) noexcept {
    mutate("schema-2 raw factor record failed", [&]() {
      CapturedTrack &track = find_track(feature_id);
      if (track.raw_available || H_x.rows() <= 0 || H_x.cols() <= 0 ||
          H_f.rows() != H_x.rows() || H_f.cols() != 3 ||
          residual.rows() != H_x.rows() || residual.cols() != 1 ||
          !H_x.allFinite() || !H_f.allFinite() || !residual.allFinite()) {
        throw std::invalid_argument("schema-2 raw factor is invalid");
      }
      track.H_x = H_x;
      track.H_f = H_f;
      track.residual = residual;
      track.layout = capture_layout(order, H_x.cols());
      track.jacobian_duration_ns = duration_ns;
      track.raw_available = true;
      track.lifecycle = TrackLifecycle::kRawAvailable;
    });
  }

  void record_reduction(std::uint64_t feature_id,
                        UpdaterOptions::LandmarkElimination reducer,
                        const SchurReductionResult *schur,
                        const Eigen::MatrixXd &reduced_H,
                        const Eigen::VectorXd &reduced_residual,
                        std::uint64_t duration_ns) noexcept {
    mutate("schema-2 reduction record failed", [&]() {
      CapturedTrack &track = find_track(feature_id);
      if (!track.raw_available || track.reduction_recorded) {
        throw std::logic_error("schema-2 reduction lifecycle is invalid");
      }
      track.reducer = reducer == UpdaterOptions::LandmarkElimination::SCHUR
                          ? 2U
                          : 1U;
      track.reduction_duration_ns = duration_ns;
      track.reduction_recorded = true;
      bool accepted = false;
      if (reducer == UpdaterOptions::LandmarkElimination::SCHUR) {
        if (!schur) {
          throw std::invalid_argument("schema-2 Schur result is absent");
        }
        track.reduction_status = static_cast<std::uint64_t>(schur->status);
        track.reduction_stage = static_cast<std::uint64_t>(schur->stage);
        track.singular_values_available = schur->singular_values_available;
        if (schur->singular_values_available) {
          track.singular_values = schur->singular_values;
          track.numerical_rank = 0;
          const double largest = schur->singular_values(0);
          const double floor = static_cast<double>(
              std::max<Eigen::Index>(track.H_f.rows(), 3)) *
              std::numeric_limits<double>::epsilon() * largest;
          for (Eigen::Index index = 0; index < 3; ++index) {
            if (schur->singular_values(index) > floor) {
              ++track.numerical_rank;
            }
          }
        }
        track.singular_ratio_available = schur->singular_ratio_available;
        if (schur->singular_ratio_available) {
          track.singular_ratio = schur->singular_ratio;
        }
        accepted = schur->accepted();
      } else {
        // Nullspace has no production SVD guard in this path.
        track.reduction_status = 0U;
        track.reduction_stage = 0U;
        accepted = reduced_H.rows() > 0;
      }
      if (accepted) {
        if (reduced_H.rows() != reduced_residual.rows() ||
            reduced_H.cols() != track.H_x.cols() ||
            !reduced_H.allFinite() || !reduced_residual.allFinite() ||
            !std::isfinite(options_.sigma_pix) ||
            !(options_.sigma_pix > 0.0)) {
          throw std::invalid_argument("schema-2 reduced factor is invalid");
        }
        track.A_f = reduced_H.array() / options_.sigma_pix;
        track.b_f = reduced_residual.array() / options_.sigma_pix;
        if (!track.A_f.allFinite() || !track.b_f.allFinite()) {
          throw std::invalid_argument("schema-2 whitened factor is nonfinite");
        }
      } else {
        track.lifecycle = TrackLifecycle::kReductionRejected;
      }
    });
  }

  void record_gate(std::uint64_t feature_id,
                   const CP2FeatureGateResult &gate,
                   std::uint64_t duration_ns) noexcept {
    mutate("schema-2 gate record failed", [&]() {
      CapturedTrack &track = find_track(feature_id);
      if (!track.reduction_recorded || track.gate_recorded) {
        throw std::logic_error("schema-2 gate lifecycle is invalid");
      }
      track.gate_recorded = true;
      track.gate_stage = static_cast<std::uint64_t>(gate.stage);
      track.gate_dof = static_cast<std::int64_t>(gate.degrees_of_freedom);
      track.nis_available = gate.chi2_available;
      track.nis = gate.chi2_available ? gate.chi2 : 0.0;
      track.gate_threshold_available = gate.threshold_available;
      track.gate_threshold = gate.threshold_available ? gate.threshold : 0.0;
      track.evidence_decision_available = gate.evidence_decision_available;
      track.evidence_accept = gate.evidence_accept;
      track.lifecycle_accept = gate.lifecycle_accept;
      track.gating_duration_ns = duration_ns;
      track.lifecycle = gate.lifecycle_accept ? TrackLifecycle::kGateAccepted
                                              : TrackLifecycle::kGateRejected;
    });
  }

  void record_accumulation(std::uint64_t feature_id,
                           std::uint64_t global_row_start,
                           std::uint64_t global_row_count,
                           std::uint64_t duration_ns) noexcept {
    mutate("schema-2 accumulation record failed", [&]() {
      CapturedTrack &track = find_track(feature_id);
      if (!track.gate_recorded || !track.lifecycle_accept ||
          track.accepted_for_global) {
        throw std::logic_error("schema-2 accumulation lifecycle is invalid");
      }
      track.accepted_for_global = true;
      track.global_row_start = global_row_start;
      track.global_row_count = global_row_count;
      track.accumulation_duration_ns = duration_ns;
      track.lifecycle = TrackLifecycle::kAccumulated;
    });
  }

  void record_selected(
      const std::vector<std::uint64_t> &accepted_ids,
      const std::vector<std::shared_ptr<ov_type::Type>> &order,
      const Eigen::MatrixXd &H, const Eigen::VectorXd &residual,
      const Eigen::MatrixXd &R, std::uint64_t duration_ns) noexcept {
    mutate("schema-2 selected system record failed", [&]() {
      if (current_->selected.available) {
        throw std::logic_error("schema-2 selected system recorded twice");
      }
      current_->accepted_ids = accepted_ids;
      current_->selected.available = true;
      current_->selected.duration_ns = duration_ns;
      current_->selected.layout = capture_layout(order, H.cols());
      current_->selected.H = H;
      current_->selected.residual = residual;
      current_->selected.R = R;
    });
  }

  void record_compressed(
      const std::vector<std::shared_ptr<ov_type::Type>> &order,
      const Eigen::MatrixXd &H, const Eigen::VectorXd &residual,
      const Eigen::MatrixXd &R, std::uint64_t duration_ns) noexcept {
    mutate("schema-2 compressed system record failed", [&]() {
      if (current_->compressed.available) {
        throw std::logic_error("schema-2 compressed system recorded twice");
      }
      current_->compressed.available = true;
      current_->compressed.duration_ns = duration_ns;
      current_->compressed.layout = capture_layout(order, H.cols());
      current_->compressed.H = H;
      current_->compressed.residual = residual;
      current_->compressed.R = R;
    });
  }

  void record_posterior(const MSCKFUpdatePreviewResult &preview,
                        bool global_gate_applied,
                        bool global_nis_available, double global_nis,
                        bool global_gate_accepted,
                        std::uint64_t duration_ns) noexcept {
    mutate("schema-2 posterior record failed", [&]() {
      if (current_->posterior_recorded ||
          (global_nis_available && !std::isfinite(global_nis))) {
        throw std::invalid_argument("schema-2 posterior metadata is invalid");
      }
      current_->posterior_recorded = true;
      current_->preview_status =
          static_cast<std::uint64_t>(preview.diagnostics.status);
      current_->preview_stage =
          static_cast<std::uint64_t>(preview.diagnostics.stage);
      current_->global_gate_applied = global_gate_applied;
      current_->global_nis_available = global_nis_available;
      current_->global_nis = global_nis_available ? global_nis : 0.0;
      current_->global_gate_accepted = global_gate_accepted;
      current_->preview_duration_ns = duration_ns;
      if (preview.accepted()) {
        current_->dx = preview.dx;
        current_->P_plus = preview.P_plus;
      }
    });
  }

  void record_terminal(UpdateEnvelopeTerminalStatus status,
                       std::uint64_t reason,
                       std::uint64_t mean_commit_count,
                       std::uint64_t covariance_commit_count,
                       std::uint64_t feature_finalization_count,
                       std::uint64_t commit_duration_ns) noexcept {
    mutate("schema-2 terminal record failed", [&]() {
      if (current_->terminal_status != UpdateEnvelopeTerminalStatus::kActive ||
          status == UpdateEnvelopeTerminalStatus::kActive) {
        throw std::logic_error("schema-2 terminal recorded twice or active");
      }
      current_->terminal_status = status;
      current_->terminal_reason = reason;
      current_->mean_commit_count = mean_commit_count;
      current_->covariance_commit_count = covariance_commit_count;
      current_->feature_finalization_count = feature_finalization_count;
      current_->commit_duration_ns = commit_duration_ns;
    });
  }

  void finish(const UpdateEnvelopeCallbackCosts &costs,
              UpdateEnvelopeTerminalStatus fallback_status,
              std::uint64_t fallback_reason) noexcept {
    if (!active_ || !current_) {
      return;
    }
    try {
      current_->callback_costs = costs;
      if (current_->terminal_status == UpdateEnvelopeTerminalStatus::kActive) {
        if (fallback_status == UpdateEnvelopeTerminalStatus::kActive) {
          throw std::logic_error("schema-2 callback has no terminal status");
        }
        current_->terminal_status = fallback_status;
        current_->terminal_reason = fallback_reason;
      }
      validate(*current_);
      const CaptureClock::time_point serialization_start =
          CaptureClock::now();
      EncodedEnvelopePayload encoded = encode(*current_);
      const std::uint64_t serialization_duration_ns =
          elapsed_ns(serialization_start, CaptureClock::now());
      if (serialization_duration_ns == 0U ||
          encoded.bytes.size() >
              static_cast<std::size_t>(
                  std::numeric_limits<std::uint64_t>::max())) {
        throw std::overflow_error(
            "schema-2 serialization metadata is invalid");
      }
      patch_u64(encoded.bytes, encoded.serialization_duration_offset,
                serialization_duration_ns);
      patch_u64(encoded.bytes, encoded.encoded_bytes_offset,
                static_cast<std::uint64_t>(encoded.bytes.size()));
      CP2Sha256 digest;
      digest.Update(encoded.bytes);
      const auto checksum = digest.Digest();
      const auto size =
          encode_u64(static_cast<std::uint64_t>(encoded.bytes.size()));
      if (!write_with_digest(size.data(), size.size()) ||
          !write_with_digest(encoded.bytes.data(), encoded.bytes.size()) ||
          !write_with_digest(checksum.data(), checksum.size())) {
        throw std::runtime_error("schema-2 envelope frame write failed");
      }
      current_.reset();
      current_state_.reset();
      ++envelope_count_;
    } catch (const std::exception &exception) {
      disable(exception.what());
    } catch (...) {
      disable("schema-2 callback finish failed");
    }
  }

private:
  template <typename Function>
  void mutate(const char *unknown_reason, Function function) noexcept {
    if (!active_ || !current_) {
      return;
    }
    const CaptureClock::time_point capture_start = CaptureClock::now();
    try {
      function();
      add_record_overhead(
          elapsed_ns(capture_start, CaptureClock::now()));
    } catch (const std::exception &exception) {
      disable(exception.what());
    } catch (...) {
      disable(unknown_reason);
    }
  }

  void add_record_overhead(std::uint64_t duration_ns) {
    if (!current_) {
      throw std::logic_error("schema-2 record overhead has no callback");
    }
    std::uint64_t updated = 0U;
    if (!checked_add_u64(current_->record_construction_duration_ns,
                         duration_ns, updated)) {
      throw std::overflow_error("schema-2 record overhead overflows u64");
    }
    current_->record_construction_duration_ns = updated;
  }

  CapturedTrack &find_track(std::uint64_t feature_id) {
    const auto found = current_->track_by_id.find(feature_id);
    if (found == current_->track_by_id.end() ||
        found->second >= current_->tracks.size()) {
      throw std::invalid_argument("schema-2 track is outside candidate population");
    }
    return current_->tracks[found->second];
  }

  void validate(const CapturedEnvelope &envelope) const {
    const Eigen::Index n = envelope.prior.filter.covariance.rows();
    if (!std::isfinite(envelope.camera_timestamp) || n <= 0 ||
        envelope.prior.filter.covariance.cols() != n ||
        !envelope.prior.filter.covariance.allFinite() ||
        envelope.prior.filter.state_blocks.size() !=
            envelope.prior.nominal_blocks.size() ||
        envelope.prior.semantic_blocks.size() !=
            envelope.prior.nominal_blocks.size() ||
        !finite_nonnegative(envelope.callback_costs.tracking_seconds) ||
        !finite_nonnegative(envelope.callback_costs.propagation_seconds) ||
        !finite_nonnegative(envelope.callback_costs.msckf_seconds) ||
        !finite_nonnegative(envelope.callback_costs.slam_update_seconds) ||
        !finite_nonnegative(envelope.callback_costs.slam_delay_seconds) ||
        !finite_nonnegative(envelope.callback_costs.finalization_seconds) ||
        !finite_nonnegative(envelope.callback_costs.total_seconds)) {
      throw std::invalid_argument("schema-2 envelope prior/cost is invalid");
    }
    const auto &timeline =
        envelope.callback_costs.stage_end_offsets_seconds;
    if (!envelope.callback_costs.stage_timestamps_available ||
        std::signbit(timeline[0]) || timeline[0] != 0.0) {
      throw std::invalid_argument(
          "schema-2 callback stage timestamps are unavailable");
    }
    for (std::size_t index = 0; index < timeline.size(); ++index) {
      if (!finite_nonnegative(timeline[index]) ||
          (index > 0U && timeline[index] < timeline[index - 1U])) {
        throw std::invalid_argument(
            "schema-2 callback stage timestamps are invalid");
      }
    }
    const std::array<double, 6> durations{{
        envelope.callback_costs.tracking_seconds,
        envelope.callback_costs.propagation_seconds,
        envelope.callback_costs.msckf_seconds,
        envelope.callback_costs.slam_update_seconds,
        envelope.callback_costs.slam_delay_seconds,
        envelope.callback_costs.finalization_seconds}};
    for (std::size_t index = 0; index < durations.size(); ++index) {
      if (!scaled_equal(timeline[index + 1U] - timeline[index],
                        durations[index])) {
        throw std::invalid_argument(
            "schema-2 callback duration/timestamp mismatch");
      }
    }
    if (!scaled_equal(timeline.back(),
                      envelope.callback_costs.total_seconds)) {
      throw std::invalid_argument(
          "schema-2 callback total/timestamp mismatch");
    }
    Eigen::Index expected = 0;
    for (std::size_t index = 0;
         index < envelope.prior.semantic_blocks.size(); ++index) {
      const auto &semantic = envelope.prior.semantic_blocks[index];
      const auto &nominal = envelope.prior.nominal_blocks[index];
      if (semantic.ordinal != static_cast<std::uint64_t>(index) ||
          semantic.variable_id != expected || semantic.covariance_id != expected ||
          semantic.offset != expected || semantic.size <= 0 ||
          semantic.role_flags != UINT64_C(3) ||
          nominal.covariance_id != expected || nominal.size != semantic.size ||
          !nominal.value.allFinite() || !nominal.fej.allFinite()) {
        throw std::invalid_argument("schema-2 semantic layout is invalid");
      }
      expected += semantic.size;
    }
    if (expected != n || envelope.track_by_id.size() != envelope.tracks.size() ||
        !validate_system(envelope.selected, n) ||
        !validate_system(envelope.compressed, n)) {
      throw std::invalid_argument("schema-2 envelope layout/system is invalid");
    }
    std::vector<std::uint64_t> accumulated;
    std::uint64_t expected_row = 0U;
    for (std::size_t index = 0; index < envelope.tracks.size(); ++index) {
      const CapturedTrack &track = envelope.tracks[index];
      if (track.ordinal != static_cast<std::uint64_t>(index) ||
          !track.prefilter_recorded) {
        throw std::invalid_argument("schema-2 candidate lifecycle is incomplete");
      }
      if (track.accepted_for_global) {
        if (track.global_row_start != expected_row ||
            track.global_row_count == 0U ||
            track.global_row_count >
                std::numeric_limits<std::uint64_t>::max() - expected_row) {
          throw std::invalid_argument(
              "schema-2 accepted row range is invalid");
        }
        expected_row += track.global_row_count;
        accumulated.push_back(track.feature_id);
      } else if (track.global_row_start != 0U ||
                 track.global_row_count != 0U) {
        throw std::invalid_argument(
            "schema-2 rejected track contains a global row range");
      }
    }
    if (accumulated != envelope.accepted_ids) {
      throw std::invalid_argument("schema-2 accepted feature order mismatch");
    }
    if (envelope.selected.available &&
        expected_row != static_cast<std::uint64_t>(envelope.selected.H.rows())) {
      throw std::invalid_argument("schema-2 accepted row ranges do not tile");
    }
    if (envelope.terminal_status == UpdateEnvelopeTerminalStatus::kCommitted) {
      if (!envelope.posterior_recorded || envelope.dx.rows() != n ||
          envelope.P_plus.rows() != n || envelope.P_plus.cols() != n ||
          !envelope.dx.allFinite() || !envelope.P_plus.allFinite() ||
          envelope.mean_commit_count != 1U ||
          envelope.covariance_commit_count != 1U ||
          envelope.feature_finalization_count != 1U) {
        throw std::invalid_argument("schema-2 committed posterior is incomplete");
      }
    } else if (envelope.mean_commit_count != 0U ||
               envelope.covariance_commit_count != 0U) {
      throw std::invalid_argument("schema-2 no-update contains a state commit");
    }
  }

  EncodedEnvelopePayload encode(const CapturedEnvelope &envelope) const {
    CP2CanonicalBuffer output;
    output.AppendUtf8(kDomain);
    output.AppendU64(static_cast<std::uint64_t>(UpdateEnvelopeStage::kCallback));
    output.AppendU64(envelope.update_id);
    output.AppendBinary64(envelope.camera_timestamp);
    output.AppendU64(static_cast<std::uint64_t>(envelope.terminal_status));
    output.AppendU64(envelope.terminal_reason);
    output.AppendU64(static_cast<std::uint64_t>(options_.max_visual_passes));
    output.AppendU64(options_.landmark_elimination ==
                             UpdaterOptions::LandmarkElimination::SCHUR
                         ? 2U
                         : 1U);
    append_bool(output, envelope.prior.do_fej);
    append_bool(output, envelope.prior.calibrate_camera_pose);
    append_bool(output, envelope.prior.calibrate_camera_intrinsics);
    append_bool(output, envelope.prior.calibrate_camera_timeoffset);
    append_bool(output, envelope.prior.calibrate_imu_intrinsics);
    append_bool(output, envelope.prior.calibrate_imu_g_sensitivity);
    output.AppendI64(envelope.prior.feature_representation);

    output.AppendU64(
        static_cast<std::uint64_t>(UpdateEnvelopeStage::kCallbackFinalization));
    output.AppendBinary64(envelope.callback_costs.tracking_seconds);
    output.AppendBinary64(envelope.callback_costs.propagation_seconds);
    output.AppendBinary64(envelope.callback_costs.msckf_seconds);
    output.AppendBinary64(envelope.callback_costs.slam_update_seconds);
    output.AppendBinary64(envelope.callback_costs.slam_delay_seconds);
    output.AppendBinary64(envelope.callback_costs.finalization_seconds);
    output.AppendBinary64(envelope.callback_costs.total_seconds);
    append_bool(output,
                envelope.callback_costs.stage_timestamps_available);
    for (const double stage_timestamp :
         envelope.callback_costs.stage_end_offsets_seconds) {
      output.AppendBinary64(stage_timestamp);
    }
    output.AppendU64(static_cast<std::uint64_t>(UpdateEnvelopeStage::kCaptureOutput));
    append_bool(output, true);
    output.AppendU64(envelope.record_construction_duration_ns);
    const std::size_t serialization_duration_offset = output.size();
    output.AppendU64(0U);
    const std::size_t encoded_bytes_offset = output.size();
    output.AppendU64(0U);

    output.AppendU64(static_cast<std::uint64_t>(envelope.prior.cameras.size()));
    for (const auto &camera : envelope.prior.cameras) {
      output.AppendU64(static_cast<std::uint64_t>(UpdateEnvelopeStage::kCallback));
      output.AppendU64(static_cast<std::uint64_t>(camera.camera_id));
      output.AppendU64(camera_model_code(camera.model));
      output.AppendI64(camera.width);
      output.AppendI64(camera.height);
      output.AppendI64(camera.extrinsic_id);
      output.AppendI64(camera.intrinsic_id);
      output.AppendMatrix(camera.extrinsic_value);
      output.AppendMatrix(camera.extrinsic_fej);
      output.AppendMatrix(camera.intrinsic_value);
      output.AppendMatrix(camera.intrinsic_fej);
      output.AppendMatrix(camera.cache_value);
    }

    output.AppendU64(
        static_cast<std::uint64_t>(envelope.prior.semantic_blocks.size()));
    for (std::size_t index = 0;
         index < envelope.prior.semantic_blocks.size(); ++index) {
      const auto &semantic = envelope.prior.semantic_blocks[index];
      const auto &nominal = envelope.prior.nominal_blocks[index];
      output.AppendU64(static_cast<std::uint64_t>(UpdateEnvelopeStage::kCallback));
      output.AppendU64(semantic.ordinal);
      output.AppendU64(static_cast<std::uint64_t>(semantic.block_type));
      output.AppendU64(static_cast<std::uint64_t>(semantic.key_type));
      output.AppendU64(semantic.key_u64);
      output.AppendBinary64(semantic.key_double);
      output.AppendI64(semantic.variable_id);
      output.AppendI64(semantic.covariance_id);
      output.AppendI64(semantic.offset);
      output.AppendI64(semantic.size);
      output.AppendU64(semantic.role_flags);
      output.AppendMatrix(nominal.value);
      output.AppendMatrix(nominal.fej);
    }
    output.AppendMatrix(envelope.prior.filter.covariance);

    output.AppendU64(static_cast<std::uint64_t>(envelope.tracks.size()));
    for (const CapturedTrack &track : envelope.tracks) {
      output.AppendU64(static_cast<std::uint64_t>(UpdateEnvelopeStage::kPrefilter));
      output.AppendU64(track.ordinal);
      output.AppendU64(track.feature_id);
      output.AppendU64(static_cast<std::uint64_t>(track.reason));
      output.AppendU64(static_cast<std::uint64_t>(track.lifecycle));
      output.AppendU64(track.raw_observation_count);
      output.AppendU64(track.cleaned_observation_count);
      append_bool(output, track.prefilter_recorded);
      append_bool(output, track.prefilter_accepted);
      output.AppendU64(track.prefilter_duration_ns);
      append_bool(output, track.time_range_available);
      output.AppendBinary64(track.first_timestamp);
      output.AppendBinary64(track.last_timestamp);
      output.AppendBinary64(track.track_age_seconds);
      output.AppendU64(static_cast<std::uint64_t>(track.observations.size()));
      for (const CapturedObservation &observation : track.observations) {
        output.AppendU64(observation.camera_id);
        output.AppendBinary64(observation.timestamp);
        output.AppendBinary64(observation.pixel_x);
        output.AppendBinary64(observation.pixel_y);
        output.AppendBinary64(observation.normalized_x);
        output.AppendBinary64(observation.normalized_y);
      }
      append_bool(output, track.parallax_2d_available);
      output.AppendBinary64(track.parallax_2d_rad);
      append_bool(output, track.maximum_image_motion_available);
      output.AppendBinary64(track.maximum_image_motion_px);
      append_bool(output, track.last_frame_displacement_available);
      output.AppendBinary64(track.last_frame_displacement_px);
      append_bool(output, track.final_normalized_location_available);
      output.AppendBinary64(track.final_normalized_x);
      output.AppendBinary64(track.final_normalized_y);
      append_bool(output, track.causal_motion_available);
      output.AppendU64(track.causal_motion_kind);
      output.AppendBinary64(track.causal_translation_m);
      output.AppendBinary64(track.causal_rotation_rad);

      output.AppendU64(static_cast<std::uint64_t>(UpdateEnvelopeStage::kGeometry));
      append_bool(output, track.geometry_recorded);
      append_bool(output, track.triangulation_attempted);
      append_bool(output, track.triangulation_succeeded);
      append_bool(output, track.refinement_attempted);
      append_bool(output, track.refinement_succeeded);
      append_bool(output, track.geometry_valid);
      output.AppendU64(track.geometry_duration_ns);
      output.AppendVector(track.p_FinG);
      append_bool(output, track.depth_available);
      output.AppendBinary64(track.minimum_depth);
      append_bool(output, track.parallax_3d_available);
      output.AppendBinary64(track.parallax_3d_rad);

      output.AppendU64(static_cast<std::uint64_t>(UpdateEnvelopeStage::kRawFactor));
      append_bool(output, track.raw_available);
      output.AppendU64(track.jacobian_duration_ns);
      output.AppendBinary64(options_.sigma_pix);
      output.AppendBinary64(options_.sigma_pix_sq);
      output.AppendU64(1U);
      append_layout(output, track.layout);
      output.AppendMatrix(track.H_x);
      output.AppendMatrix(track.H_f);
      output.AppendVector(track.residual);

      output.AppendU64(static_cast<std::uint64_t>(UpdateEnvelopeStage::kReduction));
      append_bool(output, track.reduction_recorded);
      output.AppendU64(track.reducer);
      output.AppendU64(track.reduction_status);
      output.AppendU64(track.reduction_stage);
      append_bool(output, track.singular_values_available);
      output.AppendVector(track.singular_values);
      output.AppendI64(track.numerical_rank);
      append_bool(output, track.singular_ratio_available);
      output.AppendBinary64(track.singular_ratio);
      output.AppendMatrix(track.A_f);
      output.AppendVector(track.b_f);
      output.AppendU64(track.reduction_duration_ns);

      output.AppendU64(static_cast<std::uint64_t>(UpdateEnvelopeStage::kGate));
      append_bool(output, track.gate_recorded);
      output.AppendU64(track.gate_stage);
      output.AppendI64(track.gate_dof);
      append_bool(output, track.nis_available);
      output.AppendBinary64(track.nis);
      append_bool(output, track.gate_threshold_available);
      output.AppendBinary64(track.gate_threshold);
      append_bool(output, track.evidence_decision_available);
      append_bool(output, track.evidence_accept);
      append_bool(output, track.lifecycle_accept);
      output.AppendU64(track.gating_duration_ns);

      output.AppendU64(static_cast<std::uint64_t>(UpdateEnvelopeStage::kAccumulation));
      append_bool(output, track.accepted_for_global);
      output.AppendU64(track.global_row_start);
      output.AppendU64(track.global_row_count);
      output.AppendU64(track.accumulation_duration_ns);
    }

    output.AppendU64(static_cast<std::uint64_t>(envelope.accepted_ids.size()));
    for (const std::uint64_t feature_id : envelope.accepted_ids) {
      output.AppendU64(feature_id);
    }
    append_system(output, UpdateEnvelopeStage::kAccumulation,
                  envelope.selected);
    append_system(output, UpdateEnvelopeStage::kCompression,
                  envelope.compressed);

    output.AppendU64(static_cast<std::uint64_t>(UpdateEnvelopeStage::kPreview));
    append_bool(output, envelope.posterior_recorded);
    output.AppendU64(envelope.preview_status);
    output.AppendU64(envelope.preview_stage);
    append_bool(output, envelope.global_gate_applied);
    append_bool(output, envelope.global_nis_available);
    output.AppendBinary64(envelope.global_nis);
    append_bool(output, envelope.global_gate_accepted);
    output.AppendVector(envelope.dx);
    output.AppendMatrix(envelope.P_plus);
    output.AppendU64(envelope.preview_duration_ns);

    output.AppendU64(static_cast<std::uint64_t>(UpdateEnvelopeStage::kCommit));
    output.AppendU64(envelope.mean_commit_count);
    output.AppendU64(envelope.covariance_commit_count);
    output.AppendU64(envelope.feature_finalization_count);
    output.AppendU64(envelope.commit_duration_ns);
    EncodedEnvelopePayload encoded;
    encoded.bytes = output.bytes();
    encoded.serialization_duration_offset = serialization_duration_offset;
    encoded.encoded_bytes_offset = encoded_bytes_offset;
    return encoded;
  }

  bool write_all(const void *data, std::size_t size) noexcept {
    const auto *bytes = static_cast<const std::uint8_t *>(data);
    std::size_t written = 0U;
    while (written < size) {
      const ssize_t result = ::write(fd_, bytes + written, size - written);
      if (result > 0) {
        written += static_cast<std::size_t>(result);
      } else if (result < 0 && errno == EINTR) {
        continue;
      } else {
        return false;
      }
    }
    return true;
  }

  bool write_with_digest(const void *data, std::size_t size) noexcept {
    if (fd_ < 0 || !write_all(data, size)) {
      return false;
    }
    try {
      file_digest_.Update(data, size);
      return true;
    } catch (...) {
      return false;
    }
  }

  void disable(const char *reason) noexcept {
    if (!failed_ && options_.capture_update_envelopes_v2) {
      PRINT_WARNING(
          YELLOW
          "[MSCKF-UPDATE-ENVELOPE-V2]: status=disabled reason=%s "
          "estimator_unchanged=1\n" RESET,
          reason == nullptr ? "unknown" : reason);
    }
    failed_ = options_.capture_update_envelopes_v2;
    active_ = false;
    current_.reset();
    current_state_.reset();
    if (fd_ >= 0) {
      (void)::close(fd_);
      fd_ = -1;
    }
  }

  void close_file() noexcept {
    if (fd_ < 0) {
      return;
    }
    // An open callback is deliberately unauthenticated: never bless a partial
    // envelope with a valid trailer.
    if (active_ && !current_) {
      const auto sentinel = encode_u64(kTrailerSentinel);
      const auto count = encode_u64(envelope_count_);
      const auto checksum = file_digest_.Digest();
      if (!write_all(sentinel.data(), sentinel.size()) ||
          !write_all(count.data(), count.size()) ||
          !write_all(checksum.data(), checksum.size())) {
        failed_ = true;
      }
    } else if (current_) {
      failed_ = true;
    }
    if (::close(fd_) != 0) {
      failed_ = true;
    }
    fd_ = -1;
    active_ = false;
  }

  UpdaterOptions options_;
  int fd_ = -1;
  bool active_ = false;
  bool failed_ = false;
  std::uint64_t envelope_count_ = 0U;
  std::unique_ptr<CapturedEnvelope> current_;
  std::shared_ptr<State> current_state_;
  CP2Sha256 file_digest_;
};

UpdateEnvelopeCaptureWriter::UpdateEnvelopeCaptureWriter(
    const UpdaterOptions &options) noexcept {
  try {
    impl_.reset(new Impl(options));
  } catch (...) {
    PRINT_WARNING(YELLOW
                  "[MSCKF-UPDATE-ENVELOPE-V2]: status=disabled "
                  "reason=owner_allocation estimator_unchanged=1\n" RESET);
  }
}

UpdateEnvelopeCaptureWriter::~UpdateEnvelopeCaptureWriter() noexcept = default;

bool UpdateEnvelopeCaptureWriter::active() const noexcept {
  return impl_ && impl_->active();
}
bool UpdateEnvelopeCaptureWriter::failed() const noexcept {
  return impl_ && impl_->failed();
}
bool UpdateEnvelopeCaptureWriter::callback_active() const noexcept {
  return impl_ && impl_->callback_active();
}
std::uint64_t UpdateEnvelopeCaptureWriter::envelope_count() const noexcept {
  return impl_ ? impl_->envelope_count() : 0U;
}
std::uint64_t
UpdateEnvelopeCaptureWriter::callback_record_overhead_ns() const noexcept {
  return impl_ ? impl_->callback_record_overhead_ns() : 0U;
}
void UpdateEnvelopeCaptureWriter::AddExternalRecordOverhead(
    std::uint64_t duration_ns) noexcept {
  if (impl_) {
    impl_->add_external_record_overhead(duration_ns);
  }
}
void UpdateEnvelopeCaptureWriter::BeginCallback(
    const std::shared_ptr<State> &state, double camera_timestamp) noexcept {
  if (impl_) {
    impl_->begin(state, camera_timestamp);
  }
}
void UpdateEnvelopeCaptureWriter::RefreshVisualPrior(
    const std::shared_ptr<State> &state) noexcept {
  if (impl_) {
    impl_->refresh_prior(state);
  }
}
void UpdateEnvelopeCaptureWriter::SetCandidates(
    const std::vector<std::shared_ptr<ov_core::Feature>> &features,
    const std::vector<UpdateEnvelopeCandidateReason> &reasons) noexcept {
  if (impl_) {
    impl_->set_candidates(features, reasons);
  }
}
void UpdateEnvelopeCaptureWriter::RecordPrefilter(
    const ov_core::Feature &feature, bool accepted,
    std::uint64_t duration_ns) noexcept {
  if (impl_) {
    impl_->record_prefilter(feature, accepted, duration_ns);
  }
}
void UpdateEnvelopeCaptureWriter::RecordGeometry(
    const std::shared_ptr<State> &state, const ov_core::Feature &feature,
    bool triangulation_attempted, bool triangulation_succeeded,
    bool refinement_attempted, bool refinement_succeeded,
    std::uint64_t duration_ns) noexcept {
  if (impl_) {
    impl_->record_geometry(state, feature, triangulation_attempted,
                           triangulation_succeeded, refinement_attempted,
                           refinement_succeeded, duration_ns);
  }
}
void UpdateEnvelopeCaptureWriter::RecordRawFactor(
    std::uint64_t feature_id,
    const std::vector<std::shared_ptr<ov_type::Type>> &order,
    const Eigen::MatrixXd &H_x, const Eigen::MatrixXd &H_f,
    const Eigen::VectorXd &residual, std::uint64_t duration_ns) noexcept {
  if (impl_) {
    impl_->record_raw(feature_id, order, H_x, H_f, residual, duration_ns);
  }
}
void UpdateEnvelopeCaptureWriter::RecordReduction(
    std::uint64_t feature_id, UpdaterOptions::LandmarkElimination reducer,
    const SchurReductionResult *schur, const Eigen::MatrixXd &reduced_H,
    const Eigen::VectorXd &reduced_residual,
    std::uint64_t duration_ns) noexcept {
  if (impl_) {
    impl_->record_reduction(feature_id, reducer, schur, reduced_H,
                            reduced_residual, duration_ns);
  }
}
void UpdateEnvelopeCaptureWriter::RecordGate(
    std::uint64_t feature_id, const CP2FeatureGateResult &gate,
    std::uint64_t duration_ns) noexcept {
  if (impl_) {
    impl_->record_gate(feature_id, gate, duration_ns);
  }
}
void UpdateEnvelopeCaptureWriter::RecordAccumulation(
    std::uint64_t feature_id, std::uint64_t global_row_start,
    std::uint64_t global_row_count, std::uint64_t duration_ns) noexcept {
  if (impl_) {
    impl_->record_accumulation(feature_id, global_row_start,
                               global_row_count, duration_ns);
  }
}
void UpdateEnvelopeCaptureWriter::RecordSelectedSystem(
    const std::vector<std::uint64_t> &accepted_ids,
    const std::vector<std::shared_ptr<ov_type::Type>> &order,
    const Eigen::MatrixXd &H, const Eigen::VectorXd &residual,
    const Eigen::MatrixXd &R, std::uint64_t duration_ns) noexcept {
  if (impl_) {
    impl_->record_selected(accepted_ids, order, H, residual, R, duration_ns);
  }
}
void UpdateEnvelopeCaptureWriter::RecordCompressedSystem(
    const std::vector<std::shared_ptr<ov_type::Type>> &order,
    const Eigen::MatrixXd &H, const Eigen::VectorXd &residual,
    const Eigen::MatrixXd &R, std::uint64_t duration_ns) noexcept {
  if (impl_) {
    impl_->record_compressed(order, H, residual, R, duration_ns);
  }
}
void UpdateEnvelopeCaptureWriter::RecordPosterior(
    const MSCKFUpdatePreviewResult &preview, bool global_gate_applied,
    bool global_nis_available, double global_nis,
    bool global_gate_accepted, std::uint64_t duration_ns) noexcept {
  if (impl_) {
    impl_->record_posterior(preview, global_gate_applied,
                            global_nis_available, global_nis,
                            global_gate_accepted, duration_ns);
  }
}
void UpdateEnvelopeCaptureWriter::RecordVisualTerminal(
    UpdateEnvelopeTerminalStatus status, std::uint64_t reason,
    std::uint64_t mean_commit_count,
    std::uint64_t covariance_commit_count,
    std::uint64_t feature_finalization_count,
    std::uint64_t commit_duration_ns) noexcept {
  if (impl_) {
    impl_->record_terminal(status, reason, mean_commit_count,
                           covariance_commit_count,
                           feature_finalization_count, commit_duration_ns);
  }
}
void UpdateEnvelopeCaptureWriter::FinishCallback(
    const UpdateEnvelopeCallbackCosts &costs,
    UpdateEnvelopeTerminalStatus fallback_status,
    std::uint64_t fallback_reason) noexcept {
  if (impl_) {
    impl_->finish(costs, fallback_status, fallback_reason);
  }
}

} // namespace ov_msckf
