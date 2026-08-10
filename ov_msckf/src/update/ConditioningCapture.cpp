/*
 * SchurVIO-Lite research-only camera conditioning capture.
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include "ConditioningCapture.h"

#include "CP2Canonical.h"
#include "SchurUpdate.h"
#include "state/State.h"
#include "types/LandmarkRepresentation.h"
#include "types/Type.h"
#include "utils/colors.h"
#include "utils/print.h"

#include <algorithm>
#include <array>
#include <cerrno>
#include <cmath>
#include <cstring>
#include <fcntl.h>
#include <fstream>
#include <limits>
#include <stdexcept>
#include <sys/stat.h>
#include <unistd.h>

#ifndef SCHURVIO_SOURCE_COMMIT
#define SCHURVIO_SOURCE_COMMIT "unknown"
#endif

namespace ov_msckf {
namespace {

constexpr std::uint64_t kSchemaVersion = 1U;
constexpr std::uint64_t kBinary64Scalar = 1U;
constexpr std::uint64_t kIsotropicPixelWhitening = 1U;
constexpr std::uint64_t kTrailerSentinel =
    std::numeric_limits<std::uint64_t>::max();
constexpr char kRecordDomain[] = "schurvio_camera_track_system_v1";
constexpr std::array<std::uint8_t, 16> kMagic{{
    'S', 'C', 'V', 'I', 'O', 'C', 'A', 'P',
    'T', 'U', 'R', 'E', '0', '0', '0', '1'}};

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

std::string sha256_file(const std::string &path) {
  std::ifstream input(path, std::ios::in | std::ios::binary);
  if (!input.is_open()) {
    throw std::runtime_error("conditioning capture cannot open config for hashing");
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
    throw std::runtime_error("conditioning capture config hash read failed");
  }
  return digest.HexDigest();
}

ConditioningCaptureCameraModel camera_model(
    const MSCKFUpdatePriorSnapshot &prior, std::size_t camera_id) noexcept {
  for (const MSCKFUpdatePriorCamera &camera : prior.cameras) {
    if (camera.camera_id != camera_id) {
      continue;
    }
    if (camera.model == MSCKFUpdatePriorCameraModel::kRadtan) {
      return ConditioningCaptureCameraModel::kRadtan;
    }
    if (camera.model == MSCKFUpdatePriorCameraModel::kEquidistant) {
      return ConditioningCaptureCameraModel::kEquidistant;
    }
    return ConditioningCaptureCameraModel::kUnknown;
  }
  return ConditioningCaptureCameraModel::kUnknown;
}

Eigen::Vector3d global_feature_position(
    const std::shared_ptr<State> &state,
    const UpdaterHelper::UpdaterHelperFeature &feature) {
  if (!ov_type::LandmarkRepresentation::is_relative_representation(
          feature.feat_representation)) {
    return feature.p_FinG;
  }
  if (feature.anchor_cam_id < 0) {
    throw std::invalid_argument("conditioning capture relative feature has no anchor");
  }
  const std::size_t camera_id =
      static_cast<std::size_t>(feature.anchor_cam_id);
  const auto calibration = state->_calib_IMUtoCAM.at(camera_id);
  const auto clone = state->_clones_IMU.at(feature.anchor_clone_timestamp);
  return clone->Rot().transpose() * calibration->Rot().transpose() *
             (feature.p_FinA - calibration->pos()) +
         clone->pos();
}

ConditioningCaptureLayoutBlock classify_block(
    const std::shared_ptr<State> &state,
    const std::shared_ptr<ov_type::Type> &variable,
    std::uint64_t local_column) {
  if (!variable || variable->id() < 0 || variable->size() <= 0) {
    throw std::invalid_argument("conditioning capture layout block is invalid");
  }
  ConditioningCaptureLayoutBlock block;
  block.local_column = local_column;
  block.covariance_column = static_cast<std::uint64_t>(variable->id());
  block.size = static_cast<std::uint64_t>(variable->size());

  for (const auto &clone : state->_clones_IMU) {
    if (clone.second.get() == variable.get()) {
      block.kind = ConditioningCaptureVariableKind::kClone;
      block.key_kind = ConditioningCaptureKeyKind::kTimestamp;
      block.key_double = clone.first;
      return block;
    }
  }
  for (const auto &calibration : state->_calib_IMUtoCAM) {
    if (calibration.second.get() == variable.get()) {
      block.kind = ConditioningCaptureVariableKind::kCameraExtrinsics;
      block.key_kind = ConditioningCaptureKeyKind::kCameraId;
      block.key_u64 = static_cast<std::uint64_t>(calibration.first);
      return block;
    }
  }
  for (const auto &intrinsics : state->_cam_intrinsics) {
    if (intrinsics.second.get() == variable.get()) {
      block.kind = ConditioningCaptureVariableKind::kCameraIntrinsics;
      block.key_kind = ConditioningCaptureKeyKind::kCameraId;
      block.key_u64 = static_cast<std::uint64_t>(intrinsics.first);
      return block;
    }
  }
  if (state->_calib_dt_CAMtoIMU.get() == variable.get()) {
    block.kind = ConditioningCaptureVariableKind::kCameraTimeOffset;
  }
  return block;
}

Eigen::MatrixXd active_covariance(
    const MSCKFUpdatePreviewSnapshot &prior,
    const std::vector<ConditioningCaptureLayoutBlock> &layout,
    Eigen::Index columns) {
  if (columns < 0 || prior.covariance.rows() != prior.covariance.cols()) {
    throw std::invalid_argument("conditioning capture prior dimensions are invalid");
  }
  Eigen::MatrixXd result = Eigen::MatrixXd::Zero(columns, columns);
  for (const ConditioningCaptureLayoutBlock &row : layout) {
    for (const ConditioningCaptureLayoutBlock &column : layout) {
      const Eigen::Index row_local = static_cast<Eigen::Index>(row.local_column);
      const Eigen::Index column_local =
          static_cast<Eigen::Index>(column.local_column);
      const Eigen::Index row_covariance =
          static_cast<Eigen::Index>(row.covariance_column);
      const Eigen::Index column_covariance =
          static_cast<Eigen::Index>(column.covariance_column);
      const Eigen::Index row_size = static_cast<Eigen::Index>(row.size);
      const Eigen::Index column_size = static_cast<Eigen::Index>(column.size);
      if (row_local < 0 || column_local < 0 || row_covariance < 0 ||
          column_covariance < 0 || row_size <= 0 || column_size <= 0 ||
          row_local > columns - row_size ||
          column_local > columns - column_size ||
          row_covariance > prior.covariance.rows() - row_size ||
          column_covariance > prior.covariance.cols() - column_size) {
        throw std::invalid_argument("conditioning capture layout exceeds prior");
      }
      result.block(row_local, column_local, row_size, column_size) =
          prior.covariance.block(row_covariance, column_covariance, row_size,
                                 column_size);
    }
  }
  return result;
}

void append_bool(CP2CanonicalBuffer &buffer, bool value) {
  buffer.AppendU64(value ? 1U : 0U);
}

bool finite_matrix(const Eigen::MatrixXd &matrix) noexcept {
  return matrix.rows() >= 0 && matrix.cols() >= 0 && matrix.allFinite();
}

} // namespace

const char *conditioning_capture_source_commit() noexcept {
  return SCHURVIO_SOURCE_COMMIT;
}

class ConditioningCaptureWriter::Impl {
public:
  explicit Impl(const UpdaterOptions &options) noexcept : options_(options) {
    if (!options_.capture_conditioning_systems) {
      return;
    }
    try {
      const std::string commit = conditioning_capture_source_commit();
      if (!(commit == "unknown" || lowercase_hex(commit, 40U))) {
        throw std::invalid_argument("conditioning capture source commit is invalid");
      }
      const std::string config_sha256 =
          sha256_file(options_.conditioning_capture_config_path);
      if (!lowercase_hex(config_sha256, 64U)) {
        throw std::invalid_argument("conditioning capture config digest is invalid");
      }
      fd_ = ::open(options_.conditioning_capture_path.c_str(),
                   O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, S_IRUSR | S_IWUSR |
                                                             S_IRGRP | S_IWGRP);
      if (fd_ < 0) {
        throw std::runtime_error("conditioning capture output open failed");
      }

      CP2CanonicalBuffer header;
      header.AppendU64(kSchemaVersion);
      header.AppendU64(kBinary64Scalar);
      header.AppendUtf8(commit);
      header.AppendUtf8(config_sha256);
      header.AppendBinary64(SchurUpdate::kMinimumSingularRatio);
      const std::array<std::uint8_t, 8> header_size =
          encode_u64(static_cast<std::uint64_t>(header.size()));
      CP2Sha256 header_digest;
      header_digest.Update(header.bytes());
      const std::array<std::uint8_t, 32> header_checksum =
          header_digest.Digest();
      if (!write_with_digest(kMagic.data(), kMagic.size()) ||
          !write_with_digest(header_size.data(), header_size.size()) ||
          !write_with_digest(header.bytes().data(), header.bytes().size()) ||
          !write_with_digest(header_checksum.data(), header_checksum.size())) {
        throw std::runtime_error("conditioning capture header write failed");
      }
      active_ = true;
    } catch (const std::exception &exception) {
      disable(exception.what());
    } catch (...) {
      disable("conditioning capture initialization failed");
    }
  }

  ~Impl() noexcept { close_file(); }

  bool active() const noexcept { return active_; }
  bool failed() const noexcept { return failed_; }
  std::uint64_t record_count() const noexcept { return record_count_; }

  void capture(const std::shared_ptr<State> &state,
               const UpdaterHelper::UpdaterHelperFeature &feature,
               const MSCKFUpdatePriorSnapshot &prior,
               const std::vector<std::shared_ptr<ov_type::Type>> &order,
               const Eigen::MatrixXd &H_x, const Eigen::MatrixXd &H_f,
               const Eigen::VectorXd &residual) noexcept {
    if (!active_) {
      return;
    }
    try {
      ConditioningCaptureRecord record;
      record.record_index = record_count_;
      if (!have_update_timestamp_ ||
          std::memcmp(&last_update_timestamp_, &prior.timestamp,
                      sizeof(double)) != 0) {
        if (have_update_timestamp_) {
          if (update_index_ == std::numeric_limits<std::uint64_t>::max()) {
            throw std::overflow_error("conditioning capture update index overflow");
          }
          ++update_index_;
        }
        have_update_timestamp_ = true;
        last_update_timestamp_ = prior.timestamp;
        feature_ordinal_ = 0U;
      }
      record.update_index = update_index_;
      record.feature_ordinal = feature_ordinal_;
      record.update_timestamp = prior.timestamp;
      record.feature_id = static_cast<std::uint64_t>(feature.featid);
      record.fej_enabled = prior.do_fej;
      record.calibration_flags =
          (prior.calibrate_camera_pose ? UINT64_C(1) : UINT64_C(0)) |
          (prior.calibrate_camera_intrinsics ? UINT64_C(2) : UINT64_C(0)) |
          (prior.calibrate_camera_timeoffset ? UINT64_C(4) : UINT64_C(0));
      record.feature_representation = prior.feature_representation;
      record.sigma_px = options_.sigma_pix;
      record.noise_variance = options_.sigma_pix_sq;
      record.minimum_singular_ratio = SchurUpdate::kMinimumSingularRatio;
      record.H_x = H_x;
      record.H_f = H_f;
      record.residual = residual;

      std::uint64_t local_column = 0U;
      record.layout.reserve(order.size());
      for (const std::shared_ptr<ov_type::Type> &variable : order) {
        ConditioningCaptureLayoutBlock block =
            classify_block(state, variable, local_column);
        if (block.size >
            std::numeric_limits<std::uint64_t>::max() - local_column) {
          throw std::overflow_error("conditioning capture local layout overflow");
        }
        local_column += block.size;
        record.layout.push_back(block);
      }
      if (local_column != static_cast<std::uint64_t>(H_x.cols())) {
        throw std::invalid_argument("conditioning capture layout is incomplete");
      }
      record.P_active = active_covariance(prior.filter, record.layout,
                                          H_x.cols());

      record.p_FinG = global_feature_position(state, feature);
      std::vector<Eigen::Vector3d> global_rays;
      record.observations.reserve(static_cast<std::size_t>(H_x.rows() / 2));
      record.minimum_depth = std::numeric_limits<double>::infinity();
      bool geometry_valid = record.p_FinG.allFinite();
      for (const auto &camera_observations : feature.timestamps) {
        const std::size_t camera_id = camera_observations.first;
        const auto calibration = state->_calib_IMUtoCAM.at(camera_id);
        for (const double timestamp : camera_observations.second) {
          const auto clone = state->_clones_IMU.at(timestamp);
          const Eigen::Matrix3d R_GtoC =
              calibration->Rot() * clone->Rot();
          const Eigen::Vector3d p_FinC =
              R_GtoC * (record.p_FinG - clone->pos()) +
              calibration->pos();
          const Eigen::Vector3d p_CinG =
              clone->pos() - R_GtoC.transpose() * calibration->pos();
          const Eigen::Vector3d ray = record.p_FinG - p_CinG;
          ConditioningCaptureObservation observation;
          observation.camera_id = static_cast<std::uint64_t>(camera_id);
          observation.timestamp = timestamp;
          observation.camera_model = camera_model(prior, camera_id);
          observation.depth = p_FinC.z();
          record.observations.push_back(observation);
          record.minimum_depth =
              std::min(record.minimum_depth, observation.depth);
          if (!std::isfinite(observation.depth) ||
              !(observation.depth > 0.0) || !ray.allFinite() ||
              !(ray.norm() > std::numeric_limits<double>::min())) {
            geometry_valid = false;
          } else {
            global_rays.push_back(ray.normalized());
          }
        }
      }
      if (record.observations.empty()) {
        geometry_valid = false;
        record.minimum_depth = 0.0;
      }
      record.maximum_parallax_rad = 0.0;
      for (std::size_t i = 0; i < global_rays.size(); ++i) {
        for (std::size_t j = i + 1U; j < global_rays.size(); ++j) {
          const double cosine =
              std::max(-1.0, std::min(1.0, global_rays[i].dot(global_rays[j])));
          record.maximum_parallax_rad =
              std::max(record.maximum_parallax_rad, std::acos(cosine));
        }
      }
      record.geometry_valid = geometry_valid;

      const SchurReductionResult guard = SchurUpdate::Reduce(
          record.H_x, record.H_f, record.residual, record.sigma_px);
      record.singular_values_available = guard.singular_values_available;
      record.singular_ratio_available = guard.singular_ratio_available;
      if (guard.singular_values_available) {
        record.singular_values = guard.singular_values;
        const double largest = guard.singular_values(0);
        const double floor =
            static_cast<double>(std::max<Eigen::Index>(H_f.rows(), 3)) *
            std::numeric_limits<double>::epsilon() * largest;
        record.numerical_rank = 0;
        for (Eigen::Index i = 0; i < guard.singular_values.rows(); ++i) {
          if (guard.singular_values(i) > floor) {
            ++record.numerical_rank;
          }
        }
      }
      if (guard.singular_ratio_available) {
        record.singular_ratio = guard.singular_ratio;
      }

      if (!ConditioningCaptureWriter::ValidateRecord(record)) {
        throw std::invalid_argument("conditioning capture record validation failed");
      }
      const std::vector<std::uint8_t> payload =
          ConditioningCaptureWriter::EncodeRecord(record);
      CP2Sha256 payload_digest;
      payload_digest.Update(payload);
      const std::array<std::uint8_t, 32> checksum = payload_digest.Digest();
      const std::array<std::uint8_t, 8> payload_size =
          encode_u64(static_cast<std::uint64_t>(payload.size()));
      if (!write_with_digest(payload_size.data(), payload_size.size()) ||
          !write_with_digest(payload.data(), payload.size()) ||
          !write_with_digest(checksum.data(), checksum.size())) {
        throw std::runtime_error("conditioning capture frame write failed");
      }
      if (record_count_ == std::numeric_limits<std::uint64_t>::max() ||
          feature_ordinal_ == std::numeric_limits<std::uint64_t>::max()) {
        throw std::overflow_error("conditioning capture record counter overflow");
      }
      ++record_count_;
      ++feature_ordinal_;
    } catch (const std::exception &exception) {
      disable(exception.what());
    } catch (...) {
      disable("conditioning capture record failed");
    }
  }

private:
  bool write_all(const void *data, std::size_t size) noexcept {
    const auto *bytes = static_cast<const std::uint8_t *>(data);
    std::size_t written = 0U;
    while (written < size) {
      const ssize_t result = ::write(fd_, bytes + written, size - written);
      if (result > 0) {
        written += static_cast<std::size_t>(result);
        continue;
      }
      if (result < 0 && errno == EINTR) {
        continue;
      }
      return false;
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
    if (!failed_ && options_.capture_conditioning_systems) {
      PRINT_WARNING(
          YELLOW
          "[MSCKF-CONDITIONING-CAPTURE]: status=disabled reason=%s "
          "estimator_unchanged=1\n" RESET,
          reason == nullptr ? "unknown" : reason);
    }
    failed_ = options_.capture_conditioning_systems;
    active_ = false;
    if (fd_ >= 0) {
      (void)::close(fd_);
      fd_ = -1;
    }
  }

  void close_file() noexcept {
    if (fd_ < 0) {
      return;
    }
    if (active_) {
      const std::array<std::uint8_t, 8> sentinel =
          encode_u64(kTrailerSentinel);
      const std::array<std::uint8_t, 8> count = encode_u64(record_count_);
      const std::array<std::uint8_t, 32> checksum = file_digest_.Digest();
      if (!write_all(sentinel.data(), sentinel.size()) ||
          !write_all(count.data(), count.size()) ||
          !write_all(checksum.data(), checksum.size())) {
        failed_ = true;
      }
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
  bool have_update_timestamp_ = false;
  double last_update_timestamp_ = 0.0;
  std::uint64_t update_index_ = 0U;
  std::uint64_t feature_ordinal_ = 0U;
  std::uint64_t record_count_ = 0U;
  CP2Sha256 file_digest_;
};

ConditioningCaptureWriter::ConditioningCaptureWriter(
    const UpdaterOptions &options) noexcept {
  try {
    impl_.reset(new Impl(options));
  } catch (...) {
    PRINT_WARNING(
        YELLOW
        "[MSCKF-CONDITIONING-CAPTURE]: status=disabled "
        "reason=allocation estimator_unchanged=1\n" RESET);
  }
}

ConditioningCaptureWriter::~ConditioningCaptureWriter() noexcept = default;

bool ConditioningCaptureWriter::active() const noexcept {
  return impl_ && impl_->active();
}

bool ConditioningCaptureWriter::failed() const noexcept {
  return impl_ && impl_->failed();
}

std::uint64_t ConditioningCaptureWriter::record_count() const noexcept {
  return impl_ ? impl_->record_count() : 0U;
}

void ConditioningCaptureWriter::TryCapture(
    const std::shared_ptr<State> &state,
    const UpdaterHelper::UpdaterHelperFeature &feature,
    const MSCKFUpdatePriorSnapshot &prior,
    const std::vector<std::shared_ptr<ov_type::Type>> &H_x_order,
    const Eigen::MatrixXd &H_x, const Eigen::MatrixXd &H_f,
    const Eigen::VectorXd &residual) noexcept {
  if (impl_) {
    impl_->capture(state, feature, prior, H_x_order, H_x, H_f, residual);
  }
}

bool ConditioningCaptureWriter::ValidateRecord(
    const ConditioningCaptureRecord &record) noexcept {
  if (!std::isfinite(record.update_timestamp) ||
      !std::isfinite(record.sigma_px) || !(record.sigma_px > 0.0) ||
      !std::isfinite(record.noise_variance) ||
      !(record.noise_variance > 0.0) ||
      !std::isfinite(record.minimum_singular_ratio) ||
      !(record.minimum_singular_ratio > 0.0) ||
      record.feature_representation < 0 || record.numerical_rank < 0 ||
      record.numerical_rank > 3 || !finite_matrix(record.H_x) ||
      !finite_matrix(record.H_f) || !record.residual.allFinite() ||
      !finite_matrix(record.P_active) || !record.p_FinG.allFinite() ||
      record.H_x.rows() <= 0 || record.H_x.cols() <= 0 ||
      record.H_f.rows() != record.H_x.rows() || record.H_f.cols() != 3 ||
      record.residual.rows() != record.H_x.rows() ||
      record.residual.cols() != 1 ||
      record.P_active.rows() != record.H_x.cols() ||
      record.P_active.cols() != record.H_x.cols() ||
      record.observations.size() >
          static_cast<std::size_t>(std::numeric_limits<Eigen::Index>::max()) ||
      static_cast<Eigen::Index>(record.observations.size()) * 2 !=
          record.H_x.rows() ||
      !std::isfinite(record.minimum_depth) ||
      !std::isfinite(record.maximum_parallax_rad) ||
      record.maximum_parallax_rad < 0.0 ||
      record.maximum_parallax_rad > 3.14159265358979323846) {
    return false;
  }
  if (record.singular_values_available &&
      !record.singular_values.allFinite()) {
    return false;
  }
  if (record.singular_ratio_available &&
      (!std::isfinite(record.singular_ratio) ||
       record.singular_ratio < 0.0)) {
    return false;
  }
  std::uint64_t expected_column = 0U;
  for (const ConditioningCaptureLayoutBlock &block : record.layout) {
    if (block.local_column != expected_column || block.size == 0U ||
        block.size > std::numeric_limits<std::uint64_t>::max() -
                         expected_column ||
        !std::isfinite(block.key_double)) {
      return false;
    }
    expected_column += block.size;
  }
  if (expected_column != static_cast<std::uint64_t>(record.H_x.cols())) {
    return false;
  }
  for (const ConditioningCaptureObservation &observation :
       record.observations) {
    if (!std::isfinite(observation.timestamp) ||
        !std::isfinite(observation.depth)) {
      return false;
    }
  }
  return true;
}

std::vector<std::uint8_t> ConditioningCaptureWriter::EncodeRecord(
    const ConditioningCaptureRecord &record) {
  if (!ValidateRecord(record)) {
    throw std::invalid_argument("cannot encode invalid conditioning capture record");
  }
  CP2CanonicalBuffer output;
  output.AppendUtf8(kRecordDomain);
  output.AppendU64(record.record_index);
  output.AppendU64(record.update_index);
  output.AppendU64(record.feature_ordinal);
  output.AppendBinary64(record.update_timestamp);
  output.AppendU64(record.feature_id);
  append_bool(output, record.geometry_valid);
  append_bool(output, record.fej_enabled);
  output.AppendU64(record.calibration_flags);
  output.AppendI64(record.feature_representation);
  output.AppendBinary64(record.sigma_px);
  output.AppendBinary64(record.noise_variance);
  output.AppendU64(kIsotropicPixelWhitening);
  output.AppendBinary64(record.minimum_singular_ratio);
  append_bool(output, record.singular_values_available);
  output.AppendVector(record.singular_values);
  output.AppendI64(record.numerical_rank);
  append_bool(output, record.singular_ratio_available);
  output.AppendBinary64(record.singular_ratio);
  output.AppendMatrix(record.H_x);
  output.AppendMatrix(record.H_f);
  output.AppendVector(record.residual);
  output.AppendMatrix(record.P_active);
  output.AppendVector(record.p_FinG);
  output.AppendU64(static_cast<std::uint64_t>(record.layout.size()));
  for (const ConditioningCaptureLayoutBlock &block : record.layout) {
    output.AppendU64(block.local_column);
    output.AppendU64(block.covariance_column);
    output.AppendU64(block.size);
    output.AppendU64(static_cast<std::uint64_t>(block.kind));
    output.AppendU64(static_cast<std::uint64_t>(block.key_kind));
    output.AppendU64(block.key_u64);
    output.AppendBinary64(block.key_double);
  }
  output.AppendU64(static_cast<std::uint64_t>(record.observations.size()));
  for (const ConditioningCaptureObservation &observation :
       record.observations) {
    output.AppendU64(observation.camera_id);
    output.AppendBinary64(observation.timestamp);
    output.AppendU64(static_cast<std::uint64_t>(observation.camera_model));
    output.AppendBinary64(observation.depth);
  }
  output.AppendBinary64(record.minimum_depth);
  output.AppendBinary64(record.maximum_parallax_rad);
  return output.bytes();
}

} // namespace ov_msckf
