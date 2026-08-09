/*
 * SchurVIO-Lite CP2 owning composite-state boundary and commit oracle.
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include "CP2CompositeState.h"

#include "cam/CamBase.h"
#include "cam/CamRadtan.h"
#include "state/State.h"
#include "types/IMU.h"
#include "types/JPLQuat.h"
#include "types/Landmark.h"
#include "types/LandmarkRepresentation.h"
#include "types/PoseJPL.h"
#include "types/Type.h"
#include "types/Vec.h"

#include <algorithm>
#include <atomic>
#include <cmath>
#include <cstring>
#include <limits>
#include <memory>
#include <typeinfo>
#include <type_traits>
#include <utility>

namespace ov_msckf {

struct CP2CompositePointerGraphData {
  struct ClonePointers {
    double timestamp = 0.0;
    std::shared_ptr<ov_type::PoseJPL> pose;
    std::shared_ptr<ov_type::JPLQuat> quaternion;
    std::shared_ptr<ov_type::Vec> position;
  };

  struct LandmarkPointers {
    std::uint64_t feature_id = 0;
    std::shared_ptr<ov_type::Landmark> landmark;
  };

  struct CameraPointers {
    std::uint64_t camera_id = 0;
    std::shared_ptr<ov_type::PoseJPL> extrinsic;
    std::shared_ptr<ov_type::Vec> intrinsic;
    std::shared_ptr<ov_core::CamBase> cache;
  };

  CP2StatePhase capture_phase = CP2StatePhase::kPhase1Precommit;
  CP2CompositeStateSnapshot captured_snapshot;
  mutable std::atomic<bool> postcommit_prepared{false};
  std::vector<std::shared_ptr<ov_type::Type>> variables;
  std::shared_ptr<ov_type::IMU> imu;
  std::shared_ptr<ov_type::PoseJPL> imu_pose;
  std::shared_ptr<ov_type::JPLQuat> imu_quaternion;
  std::shared_ptr<ov_type::Vec> imu_position;
  std::shared_ptr<ov_type::Vec> imu_velocity;
  std::shared_ptr<ov_type::Vec> imu_gyro_bias;
  std::shared_ptr<ov_type::Vec> imu_accel_bias;
  std::vector<ClonePointers> clones;
  std::vector<LandmarkPointers> landmarks;
  std::shared_ptr<ov_type::Vec> imu_dw;
  std::shared_ptr<ov_type::Vec> imu_da;
  std::shared_ptr<ov_type::Vec> imu_tg;
  std::shared_ptr<ov_type::JPLQuat> gyro_to_imu;
  std::shared_ptr<ov_type::JPLQuat> accel_to_imu;
  std::shared_ptr<ov_type::Vec> camera_time_offset;
  std::vector<CameraPointers> cameras;
};

namespace {

static_assert(sizeof(double) == sizeof(std::uint64_t), "CP2 requires binary64");
static_assert(std::numeric_limits<double>::is_iec559, "CP2 requires IEEE-754 binary64");

std::uint64_t double_bits(double value) noexcept {
  std::uint64_t bits = 0;
  std::memcpy(&bits, &value, sizeof(bits));
  return bits;
}

bool bit_equal(double left, double right) noexcept { return double_bits(left) == double_bits(right); }

bool eigen_index_to_u64(Eigen::Index value, std::uint64_t &output) noexcept {
  if (value < 0) {
    return false;
  }
  using UnsignedIndex = typename std::make_unsigned<Eigen::Index>::type;
  const UnsignedIndex unsigned_value = static_cast<UnsignedIndex>(value);
  if (sizeof(UnsignedIndex) > sizeof(std::uint64_t) &&
      unsigned_value > static_cast<UnsignedIndex>(std::numeric_limits<std::uint64_t>::max())) {
    return false;
  }
  output = static_cast<std::uint64_t>(unsigned_value);
  return true;
}

bool size_to_u64(std::size_t value, std::uint64_t &output) noexcept {
  if (sizeof(std::size_t) > sizeof(std::uint64_t) &&
      value > static_cast<std::size_t>(std::numeric_limits<std::uint64_t>::max())) {
    return false;
  }
  output = static_cast<std::uint64_t>(value);
  return true;
}

bool u64_to_eigen_index(std::uint64_t value, Eigen::Index &output) noexcept {
  if (value > static_cast<std::uint64_t>(std::numeric_limits<Eigen::Index>::max())) {
    return false;
  }
  output = static_cast<Eigen::Index>(value);
  return true;
}

bool checked_add_size(std::size_t left, std::size_t right, std::size_t &output) noexcept {
  if (right > std::numeric_limits<std::size_t>::max() - left) {
    return false;
  }
  output = left + right;
  return true;
}

bool checked_multiply_size(std::size_t left, std::size_t right,
                           std::size_t &output) noexcept {
  if (left != 0U && right > std::numeric_limits<std::size_t>::max() / left) {
    return false;
  }
  output = left * right;
  return true;
}

bool checked_add_u64(std::uint64_t left, std::uint64_t right,
                     std::uint64_t &output) noexcept {
  if (right > std::numeric_limits<std::uint64_t>::max() - left) {
    return false;
  }
  output = left + right;
  return true;
}

template <typename Derived> bool matrix_finite(const Eigen::MatrixBase<Derived> &matrix) noexcept {
  for (Eigen::Index row = 0; row < matrix.rows(); ++row) {
    for (Eigen::Index column = 0; column < matrix.cols(); ++column) {
      if (!std::isfinite(matrix.derived().coeff(row, column))) {
        return false;
      }
    }
  }
  return true;
}

template <typename Left, typename Right>
bool matrix_bit_equal(const Eigen::MatrixBase<Left> &left,
                      const Eigen::MatrixBase<Right> &right) noexcept {
  if (left.rows() != right.rows() || left.cols() != right.cols()) {
    return false;
  }
  for (Eigen::Index row = 0; row < left.rows(); ++row) {
    for (Eigen::Index column = 0; column < left.cols(); ++column) {
      if (!bit_equal(left.derived().coeff(row, column), right.derived().coeff(row, column))) {
        return false;
      }
    }
  }
  return true;
}

template <typename Source>
bool copy_matrix_noalloc(const Eigen::MatrixBase<Source> &source, Eigen::MatrixXd &destination) noexcept {
  if (source.rows() != destination.rows() || source.cols() != destination.cols()) {
    return false;
  }
  for (Eigen::Index row = 0; row < source.rows(); ++row) {
    for (Eigen::Index column = 0; column < source.cols(); ++column) {
      destination.coeffRef(row, column) = source.derived().coeff(row, column);
    }
  }
  return true;
}

bool is_unit_quaternion(const Eigen::MatrixXd &matrix) noexcept {
  if (matrix.rows() < 4 || matrix.cols() != 1) {
    return false;
  }
  Eigen::Matrix<double, 4, 1> quaternion;
  for (Eigen::Index index = 0; index < 4; ++index) {
    quaternion(index) = matrix(index, 0);
  }
  const double squared_norm = quaternion.squaredNorm();
  return std::isfinite(squared_norm) &&
         std::abs(squared_norm - 1.0) <=
             32.0 * std::numeric_limits<double>::epsilon();
}

bool is_global(CP2LandmarkRepresentation representation) noexcept {
  return representation == CP2LandmarkRepresentation::kGlobal3D ||
         representation == CP2LandmarkRepresentation::kGlobalFullInverseDepth;
}

bool is_anchored(CP2LandmarkRepresentation representation) noexcept {
  return representation == CP2LandmarkRepresentation::kAnchored3D ||
         representation == CP2LandmarkRepresentation::kAnchoredFullInverseDepth ||
         representation == CP2LandmarkRepresentation::kAnchoredMsckfInverseDepth ||
         representation == CP2LandmarkRepresentation::kAnchoredInverseDepthSingle;
}

CP2LandmarkRepresentation convert_representation(
    ov_type::LandmarkRepresentation::Representation representation) noexcept {
  using Production = ov_type::LandmarkRepresentation;
  switch (representation) {
  case Production::GLOBAL_3D:
    return CP2LandmarkRepresentation::kGlobal3D;
  case Production::GLOBAL_FULL_INVERSE_DEPTH:
    return CP2LandmarkRepresentation::kGlobalFullInverseDepth;
  case Production::ANCHORED_3D:
    return CP2LandmarkRepresentation::kAnchored3D;
  case Production::ANCHORED_FULL_INVERSE_DEPTH:
    return CP2LandmarkRepresentation::kAnchoredFullInverseDepth;
  case Production::ANCHORED_MSCKF_INVERSE_DEPTH:
    return CP2LandmarkRepresentation::kAnchoredMsckfInverseDepth;
  case Production::ANCHORED_INVERSE_DEPTH_SINGLE:
    return CP2LandmarkRepresentation::kAnchoredInverseDepthSingle;
  case Production::UNKNOWN:
    return CP2LandmarkRepresentation::kUnknown;
  }
  return CP2LandmarkRepresentation::kUnknown;
}

bool landmark_identity_equal(const CP2LandmarkIdentity &left,
                             const CP2LandmarkIdentity &right) noexcept {
  return left.feature_id == right.feature_id && left.representation == right.representation &&
         left.anchor_camera_id == right.anchor_camera_id &&
         bit_equal(left.anchor_timestamp, right.anchor_timestamp);
}

bool semantic_block_equal(const CP2SemanticStateBlock &left,
                          const CP2SemanticStateBlock &right) noexcept {
  if (left.kind != right.kind || left.covariance_id != right.covariance_id ||
      left.error_size != right.error_size) {
    return false;
  }
  if (left.kind == CP2SemanticStateKind::kCloneTheta ||
      left.kind == CP2SemanticStateKind::kClonePosition) {
    return bit_equal(left.clone_timestamp, right.clone_timestamp);
  }
  if (left.kind == CP2SemanticStateKind::kSlamLandmark) {
    return landmark_identity_equal(left.landmark, right.landmark);
  }
  return true;
}

bool active_type_equal(const CP2ActiveStateType &left,
                       const CP2ActiveStateType &right) noexcept {
  if (left.tag != right.tag || left.covariance_id != right.covariance_id ||
      left.error_size != right.error_size || !matrix_bit_equal(left.nominal, right.nominal) ||
      !matrix_bit_equal(left.fej, right.fej)) {
    return false;
  }
  if (left.tag == CP2ActiveStateTypeTag::kClone) {
    return bit_equal(left.clone_timestamp, right.clone_timestamp);
  }
  if (left.tag == CP2ActiveStateTypeTag::kSlamLandmark) {
    return landmark_identity_equal(left.landmark, right.landmark);
  }
  return true;
}

bool valid_phase(CP2StatePhase phase) noexcept {
  return cp2_state_phase_is_prior(phase) || cp2_state_phase_is_postcommit(phase);
}

bool same_payload_domain(CP2StatePhase left, CP2StatePhase right) noexcept {
  return (cp2_state_phase_is_prior(left) && cp2_state_phase_is_prior(right)) ||
         (cp2_state_phase_is_postcommit(left) && cp2_state_phase_is_postcommit(right));
}

} // namespace

const char *cp2_composite_state_status_name(CP2CompositeStateStatus status) noexcept {
  switch (status) {
  case CP2CompositeStateStatus::kAccepted:
    return "accepted";
  case CP2CompositeStateStatus::kNullState:
    return "null_state";
  case CP2CompositeStateStatus::kInvalidPhase:
    return "invalid_phase";
  case CP2CompositeStateStatus::kIncompleteStructure:
    return "incomplete_structure";
  case CP2CompositeStateStatus::kInvalidPointerGraph:
    return "invalid_pointer_graph";
  case CP2CompositeStateStatus::kInvalidActivePartition:
    return "invalid_active_partition";
  case CP2CompositeStateStatus::kInvalidSemanticPartition:
    return "invalid_semantic_partition";
  case CP2CompositeStateStatus::kInvalidIdentity:
    return "invalid_identity";
  case CP2CompositeStateStatus::kInvalidShape:
    return "invalid_shape";
  case CP2CompositeStateStatus::kNonfinite:
    return "nonfinite";
  case CP2CompositeStateStatus::kInvalidQuaternion:
    return "invalid_quaternion";
  case CP2CompositeStateStatus::kInvalidProposal:
    return "invalid_proposal";
  case CP2CompositeStateStatus::kNotPrepared:
    return "not_prepared";
  }
  return "unknown";
}

bool cp2_state_phase_is_prior(CP2StatePhase phase) noexcept {
  return phase == CP2StatePhase::kPhase0Prior || phase == CP2StatePhase::kPhase1Precommit;
}

bool cp2_state_phase_is_postcommit(CP2StatePhase phase) noexcept {
  return phase == CP2StatePhase::kPhase2ExpectedPostcommit ||
         phase == CP2StatePhase::kPhase3LivePostcommit;
}

bool CP2CompositeStateAdapter::CanonicallyEqual(const CP2CompositeStateSnapshot &left,
                                                const CP2CompositeStateSnapshot &right) noexcept {
  if (!same_payload_domain(left.phase, right.phase) ||
      !bit_equal(left.timestamp, right.timestamp) ||
      !matrix_bit_equal(left.covariance, right.covariance) ||
      left.semantic_blocks.size() != right.semantic_blocks.size() ||
      left.active_types.size() != right.active_types.size() ||
      left.fixed_imu_calibrations.size() != right.fixed_imu_calibrations.size() ||
      !matrix_bit_equal(left.time_offset_nominal, right.time_offset_nominal) ||
      !matrix_bit_equal(left.time_offset_fej, right.time_offset_fej) ||
      left.camera_calibrations.size() != right.camera_calibrations.size() ||
      left.camera_caches.size() != right.camera_caches.size()) {
    return false;
  }

  for (std::size_t index = 0; index < left.semantic_blocks.size(); ++index) {
    if (!semantic_block_equal(left.semantic_blocks[index], right.semantic_blocks[index])) {
      return false;
    }
  }
  for (std::size_t index = 0; index < left.active_types.size(); ++index) {
    if (!active_type_equal(left.active_types[index], right.active_types[index])) {
      return false;
    }
  }
  for (std::size_t index = 0; index < left.fixed_imu_calibrations.size(); ++index) {
    const CP2FixedImuCalibration &lhs = left.fixed_imu_calibrations[index];
    const CP2FixedImuCalibration &rhs = right.fixed_imu_calibrations[index];
    if (lhs.tag != rhs.tag || !matrix_bit_equal(lhs.nominal, rhs.nominal) ||
        !matrix_bit_equal(lhs.fej, rhs.fej)) {
      return false;
    }
  }
  for (std::size_t index = 0; index < left.camera_calibrations.size(); ++index) {
    const CP2CameraFixedCalibration &lhs = left.camera_calibrations[index];
    const CP2CameraFixedCalibration &rhs = right.camera_calibrations[index];
    if (lhs.camera_id != rhs.camera_id ||
        !matrix_bit_equal(lhs.extrinsic_nominal, rhs.extrinsic_nominal) ||
        !matrix_bit_equal(lhs.extrinsic_fej, rhs.extrinsic_fej) ||
        !matrix_bit_equal(lhs.intrinsic_nominal, rhs.intrinsic_nominal) ||
        !matrix_bit_equal(lhs.intrinsic_fej, rhs.intrinsic_fej)) {
      return false;
    }
  }
  for (std::size_t index = 0; index < left.camera_caches.size(); ++index) {
    const CP2CameraModelCache &lhs = left.camera_caches[index];
    const CP2CameraModelCache &rhs = right.camera_caches[index];
    if (lhs.camera_id != rhs.camera_id || lhs.width != rhs.width || lhs.height != rhs.height ||
        !matrix_bit_equal(lhs.calibration, rhs.calibration) || !matrix_bit_equal(lhs.K, rhs.K) ||
        !matrix_bit_equal(lhs.D, rhs.D)) {
      return false;
    }
  }
  return true;
}

CP2CompositeStateStatus
CP2CompositeStateAdapter::Validate(const CP2CompositeStateSnapshot &snapshot) noexcept {
  if (!valid_phase(snapshot.phase)) {
    return CP2CompositeStateStatus::kInvalidPhase;
  }
  if (snapshot.covariance.rows() <= 0 ||
      snapshot.covariance.cols() != snapshot.covariance.rows()) {
    return CP2CompositeStateStatus::kInvalidShape;
  }

  std::uint64_t covariance_dimension = 0;
  if (!eigen_index_to_u64(snapshot.covariance.rows(), covariance_dimension)) {
    return CP2CompositeStateStatus::kInvalidShape;
  }
  if (snapshot.active_types.empty()) {
    return CP2CompositeStateStatus::kInvalidActivePartition;
  }

  std::uint64_t expected_offset = 0;
  std::size_t imu_count = 0;
  std::size_t clone_count = 0;
  std::size_t landmark_count = 0;
  double previous_clone_timestamp = 0.0;
  bool have_previous_clone = false;
  for (std::size_t index = 0; index < snapshot.active_types.size(); ++index) {
    const CP2ActiveStateType &active = snapshot.active_types[index];
    std::uint64_t updated_offset = 0;
    if (active.error_size == 0 || active.covariance_id != expected_offset ||
        expected_offset > covariance_dimension ||
        !checked_add_u64(expected_offset, active.error_size, updated_offset) ||
        updated_offset > covariance_dimension) {
      return CP2CompositeStateStatus::kInvalidActivePartition;
    }
    expected_offset = updated_offset;

    if (active.nominal.cols() != 1 || active.fej.cols() != 1 ||
        active.nominal.rows() != active.fej.rows()) {
      return CP2CompositeStateStatus::kInvalidShape;
    }
    switch (active.tag) {
    case CP2ActiveStateTypeTag::kImu:
      ++imu_count;
      if (index != 0 || active.covariance_id != 0 || active.error_size != 15 ||
          active.nominal.rows() != 16) {
        return CP2CompositeStateStatus::kInvalidActivePartition;
      }
      break;
    case CP2ActiveStateTypeTag::kClone:
      ++clone_count;
      if (active.error_size != 6 || active.nominal.rows() != 7) {
        return CP2CompositeStateStatus::kInvalidShape;
      }
      if (!std::isfinite(active.clone_timestamp) ||
          (have_previous_clone && !(previous_clone_timestamp < active.clone_timestamp))) {
        return std::isfinite(active.clone_timestamp) ? CP2CompositeStateStatus::kInvalidIdentity
                                                     : CP2CompositeStateStatus::kNonfinite;
      }
      previous_clone_timestamp = active.clone_timestamp;
      have_previous_clone = true;
      break;
    case CP2ActiveStateTypeTag::kSlamLandmark: {
      ++landmark_count;
      const std::uint64_t required_size =
          active.landmark.representation == CP2LandmarkRepresentation::kAnchoredInverseDepthSingle
              ? 1U
              : 3U;
      if (active.landmark.representation == CP2LandmarkRepresentation::kUnknown ||
          active.error_size != required_size ||
          active.nominal.rows() != static_cast<Eigen::Index>(required_size)) {
        return active.landmark.representation == CP2LandmarkRepresentation::kUnknown
                   ? CP2CompositeStateStatus::kInvalidIdentity
                   : CP2CompositeStateStatus::kInvalidShape;
      }
      for (std::size_t prior = 0; prior < index; ++prior) {
        if (snapshot.active_types[prior].tag == CP2ActiveStateTypeTag::kSlamLandmark &&
            snapshot.active_types[prior].landmark.feature_id == active.landmark.feature_id) {
          return CP2CompositeStateStatus::kInvalidIdentity;
        }
      }
      break;
    }
    default:
      return CP2CompositeStateStatus::kInvalidActivePartition;
    }
  }
  if (expected_offset != covariance_dimension || imu_count != 1U) {
    return CP2CompositeStateStatus::kInvalidActivePartition;
  }

  std::size_t twice_clone_count = 0;
  std::size_t expected_semantic_count = 0;
  if (!checked_multiply_size(2U, clone_count, twice_clone_count) ||
      !checked_add_size(5U, twice_clone_count, expected_semantic_count) ||
      !checked_add_size(expected_semantic_count, landmark_count,
                        expected_semantic_count)) {
    return CP2CompositeStateStatus::kInvalidSemanticPartition;
  }
  if (snapshot.semantic_blocks.size() != expected_semantic_count) {
    return CP2CompositeStateStatus::kInvalidSemanticPartition;
  }

  std::size_t semantic_index = 0;
  const CP2SemanticStateKind imu_kinds[] = {
      CP2SemanticStateKind::kImuTheta, CP2SemanticStateKind::kImuPosition,
      CP2SemanticStateKind::kImuVelocity, CP2SemanticStateKind::kImuGyroBias,
      CP2SemanticStateKind::kImuAccelBias};
  for (std::size_t offset = 0; offset < 5U; ++offset, ++semantic_index) {
    const CP2SemanticStateBlock &block = snapshot.semantic_blocks[semantic_index];
    if (block.kind != imu_kinds[offset] || block.covariance_id != 3U * offset ||
        block.error_size != 3U) {
      return CP2CompositeStateStatus::kInvalidSemanticPartition;
    }
  }
  for (const CP2ActiveStateType &active : snapshot.active_types) {
    if (active.tag == CP2ActiveStateTypeTag::kImu) {
      continue;
    }
    if (active.tag == CP2ActiveStateTypeTag::kClone) {
      const CP2SemanticStateBlock &theta = snapshot.semantic_blocks[semantic_index++];
      const CP2SemanticStateBlock &position = snapshot.semantic_blocks[semantic_index++];
      if (!std::isfinite(theta.clone_timestamp) ||
          !std::isfinite(position.clone_timestamp)) {
        return CP2CompositeStateStatus::kNonfinite;
      }
      if (theta.kind != CP2SemanticStateKind::kCloneTheta ||
          position.kind != CP2SemanticStateKind::kClonePosition ||
          theta.covariance_id != active.covariance_id ||
          position.covariance_id != active.covariance_id + 3U || theta.error_size != 3U ||
          position.error_size != 3U || !bit_equal(theta.clone_timestamp, active.clone_timestamp) ||
          !bit_equal(position.clone_timestamp, active.clone_timestamp)) {
        return CP2CompositeStateStatus::kInvalidSemanticPartition;
      }
    } else {
      const CP2SemanticStateBlock &landmark = snapshot.semantic_blocks[semantic_index++];
      if (!std::isfinite(landmark.landmark.anchor_timestamp)) {
        return CP2CompositeStateStatus::kNonfinite;
      }
      if (landmark.kind != CP2SemanticStateKind::kSlamLandmark ||
          landmark.covariance_id != active.covariance_id ||
          landmark.error_size != active.error_size ||
          !landmark_identity_equal(landmark.landmark, active.landmark)) {
        return CP2CompositeStateStatus::kInvalidSemanticPartition;
      }
    }
  }
  if (semantic_index != snapshot.semantic_blocks.size()) {
    return CP2CompositeStateStatus::kInvalidSemanticPartition;
  }

  if (snapshot.fixed_imu_calibrations.size() != 5U) {
    return CP2CompositeStateStatus::kInvalidShape;
  }
  const CP2FixedImuCalibrationTag fixed_tags[] = {
      CP2FixedImuCalibrationTag::kDw, CP2FixedImuCalibrationTag::kDa,
      CP2FixedImuCalibrationTag::kTg, CP2FixedImuCalibrationTag::kGyroToImu,
      CP2FixedImuCalibrationTag::kAccelToImu};
  const Eigen::Index fixed_rows[] = {6, 6, 9, 4, 4};
  for (std::size_t index = 0; index < 5U; ++index) {
    const CP2FixedImuCalibration &fixed = snapshot.fixed_imu_calibrations[index];
    if (fixed.tag != fixed_tags[index] || fixed.nominal.rows() != fixed_rows[index] ||
        fixed.nominal.cols() != 1 || fixed.fej.rows() != fixed_rows[index] ||
        fixed.fej.cols() != 1) {
      return CP2CompositeStateStatus::kInvalidShape;
    }
  }
  if (snapshot.time_offset_nominal.rows() != 1 || snapshot.time_offset_nominal.cols() != 1 ||
      snapshot.time_offset_fej.rows() != 1 || snapshot.time_offset_fej.cols() != 1) {
    return CP2CompositeStateStatus::kInvalidShape;
  }

  if (snapshot.camera_calibrations.empty() ||
      snapshot.camera_calibrations.size() != snapshot.camera_caches.size()) {
    return CP2CompositeStateStatus::kInvalidShape;
  }
  for (std::size_t index = 0; index < snapshot.camera_calibrations.size(); ++index) {
    const CP2CameraFixedCalibration &calibration = snapshot.camera_calibrations[index];
    const CP2CameraModelCache &cache = snapshot.camera_caches[index];
    if (calibration.camera_id != static_cast<std::uint64_t>(index) ||
        cache.camera_id != calibration.camera_id || calibration.extrinsic_nominal.rows() != 7 ||
        calibration.extrinsic_nominal.cols() != 1 || calibration.extrinsic_fej.rows() != 7 ||
        calibration.extrinsic_fej.cols() != 1 || calibration.intrinsic_nominal.rows() != 8 ||
        calibration.intrinsic_nominal.cols() != 1 || calibration.intrinsic_fej.rows() != 8 ||
        calibration.intrinsic_fej.cols() != 1 || cache.width <= 0 || cache.height <= 0 ||
        cache.calibration.rows() != 8 || cache.calibration.cols() != 1 || cache.K.rows() != 3 ||
        cache.K.cols() != 3 || cache.D.rows() != 4 || cache.D.cols() != 1) {
      return CP2CompositeStateStatus::kInvalidShape;
    }
  }

  // All shape and inventory checks precede passing-value checks. Capture and
  // codecs remain lossless for every binary64 pattern; this validator alone
  // decides whether those bits may participate in estimator arithmetic.
  if (!std::isfinite(snapshot.timestamp) || !matrix_finite(snapshot.covariance) ||
      !matrix_finite(snapshot.time_offset_nominal) || !matrix_finite(snapshot.time_offset_fej)) {
    return CP2CompositeStateStatus::kNonfinite;
  }
  for (const CP2ActiveStateType &active : snapshot.active_types) {
    if (!matrix_finite(active.nominal) || !matrix_finite(active.fej)) {
      return CP2CompositeStateStatus::kNonfinite;
    }
    if (active.tag == CP2ActiveStateTypeTag::kSlamLandmark) {
      const CP2LandmarkIdentity &identity = active.landmark;
      if (!std::isfinite(identity.anchor_timestamp)) {
        return CP2CompositeStateStatus::kNonfinite;
      }
      if (is_global(identity.representation)) {
        if (identity.anchor_camera_id != -1 ||
            double_bits(identity.anchor_timestamp) != UINT64_C(0xbff0000000000000)) {
          return CP2CompositeStateStatus::kInvalidIdentity;
        }
      } else if (is_anchored(identity.representation)) {
        if (identity.anchor_camera_id < 0 ||
            static_cast<std::uint64_t>(identity.anchor_camera_id) >=
                snapshot.camera_calibrations.size()) {
          return CP2CompositeStateStatus::kInvalidIdentity;
        }
        bool anchor_found = false;
        for (const CP2ActiveStateType &candidate : snapshot.active_types) {
          if (candidate.tag == CP2ActiveStateTypeTag::kClone &&
              bit_equal(candidate.clone_timestamp, identity.anchor_timestamp)) {
            if (anchor_found) {
              return CP2CompositeStateStatus::kInvalidIdentity;
            }
            anchor_found = true;
          }
        }
        if (!anchor_found) {
          return CP2CompositeStateStatus::kInvalidIdentity;
        }
      } else {
        return CP2CompositeStateStatus::kInvalidIdentity;
      }
    }
  }
  for (const CP2FixedImuCalibration &fixed : snapshot.fixed_imu_calibrations) {
    if (!matrix_finite(fixed.nominal) || !matrix_finite(fixed.fej)) {
      return CP2CompositeStateStatus::kNonfinite;
    }
  }
  for (const CP2CameraFixedCalibration &calibration : snapshot.camera_calibrations) {
    if (!matrix_finite(calibration.extrinsic_nominal) ||
        !matrix_finite(calibration.extrinsic_fej) ||
        !matrix_finite(calibration.intrinsic_nominal) ||
        !matrix_finite(calibration.intrinsic_fej)) {
      return CP2CompositeStateStatus::kNonfinite;
    }
  }
  for (const CP2CameraModelCache &cache : snapshot.camera_caches) {
    if (!matrix_finite(cache.calibration) || !matrix_finite(cache.K) || !matrix_finite(cache.D)) {
      return CP2CompositeStateStatus::kNonfinite;
    }
  }

  for (const CP2ActiveStateType &active : snapshot.active_types) {
    if ((active.tag == CP2ActiveStateTypeTag::kImu ||
         active.tag == CP2ActiveStateTypeTag::kClone) &&
        (!is_unit_quaternion(active.nominal) || !is_unit_quaternion(active.fej))) {
      return CP2CompositeStateStatus::kInvalidQuaternion;
    }
  }
  for (std::size_t index = 3U; index < 5U; ++index) {
    if (!is_unit_quaternion(snapshot.fixed_imu_calibrations[index].nominal) ||
        !is_unit_quaternion(snapshot.fixed_imu_calibrations[index].fej)) {
      return CP2CompositeStateStatus::kInvalidQuaternion;
    }
  }
  for (const CP2CameraFixedCalibration &calibration : snapshot.camera_calibrations) {
    if (!is_unit_quaternion(calibration.extrinsic_nominal) ||
        !is_unit_quaternion(calibration.extrinsic_fej)) {
      return CP2CompositeStateStatus::kInvalidQuaternion;
    }
  }
  return CP2CompositeStateStatus::kAccepted;
}

namespace {

bool pose_parent_consistent(const std::shared_ptr<ov_type::PoseJPL> &pose) noexcept {
  if (!pose || !pose->q() || !pose->p() || pose->size() != 6 || pose->q()->size() != 3 ||
      pose->p()->size() != 3 || pose->value().rows() != 7 || pose->value().cols() != 1 ||
      pose->fej().rows() != 7 || pose->fej().cols() != 1 || pose->q()->value().rows() != 4 ||
      pose->q()->value().cols() != 1 || pose->q()->fej().rows() != 4 ||
      pose->q()->fej().cols() != 1 || pose->p()->value().rows() != 3 ||
      pose->p()->value().cols() != 1 || pose->p()->fej().rows() != 3 ||
      pose->p()->fej().cols() != 1 || pose->id() < 0 ||
      pose->id() > std::numeric_limits<int>::max() - 3) {
    return false;
  }
  if (pose->q()->id() != pose->id() || pose->p()->id() != pose->id() + 3) {
    return false;
  }
  for (Eigen::Index index = 0; index < 4; ++index) {
    if (!bit_equal(pose->value()(index, 0), pose->q()->value()(index, 0)) ||
        !bit_equal(pose->fej()(index, 0), pose->q()->fej()(index, 0))) {
      return false;
    }
  }
  for (Eigen::Index index = 0; index < 3; ++index) {
    if (!bit_equal(pose->value()(index + 4, 0), pose->p()->value()(index, 0)) ||
        !bit_equal(pose->fej()(index + 4, 0), pose->p()->fej()(index, 0))) {
      return false;
    }
  }
  return true;
}

bool imu_parent_consistent(const std::shared_ptr<ov_type::IMU> &imu) noexcept {
  if (!imu || !imu->pose() || !imu->q() || !imu->p() || !imu->v() || !imu->bg() ||
      !imu->ba() || imu->size() != 15 || imu->value().rows() != 16 ||
      imu->value().cols() != 1 || imu->fej().rows() != 16 || imu->fej().cols() != 1 ||
      imu->id() < 0 || imu->id() > std::numeric_limits<int>::max() - 12 ||
      !pose_parent_consistent(imu->pose())) {
    return false;
  }
  if (imu->pose()->id() != imu->id() || imu->q()->id() != imu->id() ||
      imu->p()->id() != imu->id() + 3 || imu->v()->id() != imu->id() + 6 ||
      imu->bg()->id() != imu->id() + 9 || imu->ba()->id() != imu->id() + 12 ||
      imu->v()->size() != 3 || imu->bg()->size() != 3 || imu->ba()->size() != 3) {
    return false;
  }
  const std::shared_ptr<ov_type::Vec> vectors[] = {imu->v(), imu->bg(), imu->ba()};
  const Eigen::Index parent_offsets[] = {7, 10, 13};
  for (std::size_t vector_index = 0; vector_index < 3U; ++vector_index) {
    const std::shared_ptr<ov_type::Vec> &vector = vectors[vector_index];
    if (vector->value().rows() != 3 || vector->value().cols() != 1 ||
        vector->fej().rows() != 3 || vector->fej().cols() != 1) {
      return false;
    }
    for (Eigen::Index coefficient = 0; coefficient < 3; ++coefficient) {
      if (!bit_equal(imu->value()(parent_offsets[vector_index] + coefficient, 0),
                     vector->value()(coefficient, 0)) ||
          !bit_equal(imu->fej()(parent_offsets[vector_index] + coefficient, 0),
                     vector->fej()(coefficient, 0))) {
        return false;
      }
    }
  }
  for (Eigen::Index coefficient = 0; coefficient < 7; ++coefficient) {
    if (!bit_equal(imu->value()(coefficient, 0), imu->pose()->value()(coefficient, 0)) ||
        !bit_equal(imu->fej()(coefficient, 0), imu->pose()->fej()(coefficient, 0))) {
      return false;
    }
  }
  return true;
}

bool push_unique_role(std::vector<const void *> &roles, const void *address) {
  if (address == nullptr || std::find(roles.begin(), roles.end(), address) != roles.end()) {
    return false;
  }
  roles.push_back(address);
  return true;
}

CP2LandmarkIdentity landmark_identity(std::uint64_t feature_id,
                                     const ov_type::Landmark &landmark) noexcept {
  CP2LandmarkIdentity identity;
  identity.feature_id = feature_id;
  identity.representation = convert_representation(landmark._feat_representation);
  identity.anchor_camera_id = static_cast<std::int64_t>(landmark._anchor_cam_id);
  identity.anchor_timestamp = landmark._anchor_clone_timestamp;
  return identity;
}

void append_imu_semantic_blocks(std::vector<CP2SemanticStateBlock> &blocks,
                                ov_type::IMU &imu) {
  const CP2SemanticStateKind kinds[] = {
      CP2SemanticStateKind::kImuTheta, CP2SemanticStateKind::kImuPosition,
      CP2SemanticStateKind::kImuVelocity, CP2SemanticStateKind::kImuGyroBias,
      CP2SemanticStateKind::kImuAccelBias};
  const int ids[] = {imu.q()->id(), imu.p()->id(), imu.v()->id(), imu.bg()->id(),
                     imu.ba()->id()};
  for (std::size_t index = 0; index < 5U; ++index) {
    CP2SemanticStateBlock block;
    block.kind = kinds[index];
    block.covariance_id = static_cast<std::uint64_t>(ids[index]);
    block.error_size = 3U;
    blocks.push_back(std::move(block));
  }
}

void append_clone_semantic_blocks(std::vector<CP2SemanticStateBlock> &blocks,
                                  ov_type::PoseJPL &clone, double timestamp) {
  CP2SemanticStateBlock theta;
  theta.kind = CP2SemanticStateKind::kCloneTheta;
  theta.covariance_id = static_cast<std::uint64_t>(clone.q()->id());
  theta.error_size = 3U;
  theta.clone_timestamp = timestamp;
  blocks.push_back(std::move(theta));

  CP2SemanticStateBlock position;
  position.kind = CP2SemanticStateKind::kClonePosition;
  position.covariance_id = static_cast<std::uint64_t>(clone.p()->id());
  position.error_size = 3U;
  position.clone_timestamp = timestamp;
  blocks.push_back(std::move(position));
}

bool fixed_vec_shape(const std::shared_ptr<ov_type::Type> &type, Eigen::Index rows) noexcept {
  return type && type->id() == -1 && type->value().rows() == rows && type->value().cols() == 1 &&
         type->fej().rows() == rows && type->fej().cols() == 1;
}

} // namespace

CP2CompositeStateStatus
CP2CompositeStateAdapter::Capture(const std::shared_ptr<State> &state, CP2StatePhase phase,
                                  CP2CompositeStateCapture &output) {
  if (!state) {
    return CP2CompositeStateStatus::kNullState;
  }
  if (!cp2_state_phase_is_prior(phase)) {
    return CP2CompositeStateStatus::kInvalidPhase;
  }

  try {
    CP2CompositeStateCapture captured;
    captured.snapshot.phase = phase;
    captured.snapshot.timestamp = state->_timestamp;
    captured.snapshot.covariance = state->_Cov;

    // The frozen CP2 profile treats every calibration Type as fixed. An active
    // calibration cannot be omitted or recast as an unknown active block.
    if (state->_options.do_calib_imu_intrinsics ||
        state->_options.do_calib_imu_g_sensitivity ||
        state->_options.do_calib_camera_timeoffset || state->_options.do_calib_camera_pose ||
        state->_options.do_calib_camera_intrinsics || state->_options.num_cameras <= 0) {
      return CP2CompositeStateStatus::kInvalidActivePartition;
    }
    if (state->_Cov.rows() <= 0 || state->_Cov.cols() != state->_Cov.rows() ||
        state->_variables.empty() || !imu_parent_consistent(state->_imu) ||
        state->_imu->id() != 0) {
      return CP2CompositeStateStatus::kIncompleteStructure;
    }

    const std::shared_ptr<ov_type::Type> fixed_types[] = {
        state->_calib_imu_dw, state->_calib_imu_da, state->_calib_imu_tg,
        state->_calib_imu_GYROtoIMU, state->_calib_imu_ACCtoIMU,
        state->_calib_dt_CAMtoIMU};
    const Eigen::Index fixed_rows[] = {6, 6, 9, 4, 4, 1};
    for (std::size_t index = 0; index < 6U; ++index) {
      if (!fixed_vec_shape(fixed_types[index], fixed_rows[index])) {
        return CP2CompositeStateStatus::kIncompleteStructure;
      }
    }

    if (state->_calib_IMUtoCAM.size() != static_cast<std::size_t>(state->_options.num_cameras) ||
        state->_cam_intrinsics.size() != static_cast<std::size_t>(state->_options.num_cameras) ||
        state->_cam_intrinsics_cameras.size() !=
            static_cast<std::size_t>(state->_options.num_cameras)) {
      return CP2CompositeStateStatus::kIncompleteStructure;
    }

    const std::size_t camera_count = static_cast<std::size_t>(state->_options.num_cameras);
    std::size_t three_clone_roles = 0;
    std::size_t three_camera_roles = 0;
    std::size_t role_capacity = 0;
    std::size_t active_expected = 0;
    std::size_t two_clone_blocks = 0;
    std::size_t semantic_capacity = 0;
    if (!checked_multiply_size(3U, state->_clones_IMU.size(), three_clone_roles) ||
        !checked_multiply_size(3U, camera_count, three_camera_roles) ||
        !checked_add_size(13U, three_clone_roles, role_capacity) ||
        !checked_add_size(role_capacity, state->_features_SLAM.size(), role_capacity) ||
        !checked_add_size(role_capacity, three_camera_roles, role_capacity) ||
        !checked_add_size(1U, state->_clones_IMU.size(), active_expected) ||
        !checked_add_size(active_expected, state->_features_SLAM.size(), active_expected) ||
        !checked_multiply_size(2U, state->_clones_IMU.size(), two_clone_blocks) ||
        !checked_add_size(5U, two_clone_blocks, semantic_capacity) ||
        !checked_add_size(semantic_capacity, state->_features_SLAM.size(), semantic_capacity)) {
      return CP2CompositeStateStatus::kIncompleteStructure;
    }

    auto graph = std::make_shared<CP2CompositePointerGraphData>();
    graph->capture_phase = phase;
    graph->variables.reserve(state->_variables.size());
    graph->clones.reserve(state->_clones_IMU.size());
    graph->landmarks.reserve(state->_features_SLAM.size());
    graph->cameras.reserve(camera_count);
    graph->imu = state->_imu;
    graph->imu_pose = state->_imu->pose();
    graph->imu_quaternion = state->_imu->q();
    graph->imu_position = state->_imu->p();
    graph->imu_velocity = state->_imu->v();
    graph->imu_gyro_bias = state->_imu->bg();
    graph->imu_accel_bias = state->_imu->ba();
    graph->imu_dw = state->_calib_imu_dw;
    graph->imu_da = state->_calib_imu_da;
    graph->imu_tg = state->_calib_imu_tg;
    graph->gyro_to_imu = state->_calib_imu_GYROtoIMU;
    graph->accel_to_imu = state->_calib_imu_ACCtoIMU;
    graph->camera_time_offset = state->_calib_dt_CAMtoIMU;

    std::vector<const void *> distinct_roles;
    distinct_roles.reserve(role_capacity);
    const void *imu_roles[] = {
        state->_imu.get(),          state->_imu->pose().get(), state->_imu->q().get(),
        state->_imu->p().get(),     state->_imu->v().get(),    state->_imu->bg().get(),
        state->_imu->ba().get(),    state->_calib_imu_dw.get(),
        state->_calib_imu_da.get(), state->_calib_imu_tg.get(),
        state->_calib_imu_GYROtoIMU.get(), state->_calib_imu_ACCtoIMU.get(),
        state->_calib_dt_CAMtoIMU.get()};
    for (const void *role : imu_roles) {
      if (!push_unique_role(distinct_roles, role)) {
        return CP2CompositeStateStatus::kInvalidPointerGraph;
      }
    }

    for (const auto &entry : state->_clones_IMU) {
      const std::shared_ptr<ov_type::PoseJPL> &clone = entry.second;
      if (!pose_parent_consistent(clone) || !push_unique_role(distinct_roles, clone.get()) ||
          !push_unique_role(distinct_roles, clone->q().get()) ||
          !push_unique_role(distinct_roles, clone->p().get())) {
        return CP2CompositeStateStatus::kInvalidPointerGraph;
      }
      CP2CompositePointerGraphData::ClonePointers pointers;
      pointers.timestamp = entry.first;
      pointers.pose = clone;
      pointers.quaternion = clone->q();
      pointers.position = clone->p();
      graph->clones.push_back(std::move(pointers));
    }

    for (const auto &entry : state->_features_SLAM) {
      if (!entry.second || entry.first != entry.second->_featid ||
          !push_unique_role(distinct_roles, entry.second.get())) {
        return entry.second && entry.first != entry.second->_featid
                   ? CP2CompositeStateStatus::kInvalidIdentity
                   : CP2CompositeStateStatus::kInvalidPointerGraph;
      }
    }

    for (int camera_index = 0; camera_index < state->_options.num_cameras; ++camera_index) {
      const std::size_t camera_id = static_cast<std::size_t>(camera_index);
      const auto extrinsic_it = state->_calib_IMUtoCAM.find(camera_id);
      const auto intrinsic_it = state->_cam_intrinsics.find(camera_id);
      const auto cache_it = state->_cam_intrinsics_cameras.find(camera_id);
      if (extrinsic_it == state->_calib_IMUtoCAM.end() ||
          intrinsic_it == state->_cam_intrinsics.end() ||
          cache_it == state->_cam_intrinsics_cameras.end() || !extrinsic_it->second ||
          !intrinsic_it->second || !cache_it->second || extrinsic_it->second->id() != -1 ||
          intrinsic_it->second->id() != -1 || extrinsic_it->second->value().rows() != 7 ||
          extrinsic_it->second->value().cols() != 1 || extrinsic_it->second->fej().rows() != 7 ||
          extrinsic_it->second->fej().cols() != 1 || intrinsic_it->second->value().rows() != 8 ||
          intrinsic_it->second->value().cols() != 1 || intrinsic_it->second->fej().rows() != 8 ||
          intrinsic_it->second->fej().cols() != 1 ||
          typeid(*cache_it->second) != typeid(ov_core::CamRadtan) ||
          cache_it->second->cp2_cache_value().rows() != 8 ||
          cache_it->second->cp2_cache_value().cols() != 1 ||
          !push_unique_role(distinct_roles, extrinsic_it->second.get()) ||
          !push_unique_role(distinct_roles, intrinsic_it->second.get()) ||
          !push_unique_role(distinct_roles, cache_it->second.get())) {
        return CP2CompositeStateStatus::kIncompleteStructure;
      }

      CP2CompositePointerGraphData::CameraPointers pointers;
      pointers.camera_id = static_cast<std::uint64_t>(camera_id);
      pointers.extrinsic = extrinsic_it->second;
      pointers.intrinsic = intrinsic_it->second;
      pointers.cache = cache_it->second;
      graph->cameras.push_back(std::move(pointers));

      CP2CameraFixedCalibration camera_calibration;
      camera_calibration.camera_id = static_cast<std::uint64_t>(camera_id);
      camera_calibration.extrinsic_nominal = extrinsic_it->second->value();
      camera_calibration.extrinsic_fej = extrinsic_it->second->fej();
      camera_calibration.intrinsic_nominal = intrinsic_it->second->value();
      camera_calibration.intrinsic_fej = intrinsic_it->second->fej();
      captured.snapshot.camera_calibrations.push_back(std::move(camera_calibration));

      CP2CameraModelCache cache;
      cache.camera_id = static_cast<std::uint64_t>(camera_id);
      cache.width = static_cast<std::int64_t>(cache_it->second->cp2_cache_width());
      cache.height = static_cast<std::int64_t>(cache_it->second->cp2_cache_height());
      cache.calibration = cache_it->second->cp2_cache_value();
      cache.K.resize(3, 3);
      cache.D.resize(4, 1);
      const cv::Matx33d &K = cache_it->second->cp2_cache_K();
      const cv::Vec4d &D = cache_it->second->cp2_cache_D();
      for (Eigen::Index row = 0; row < 3; ++row) {
        for (Eigen::Index column = 0; column < 3; ++column) {
          cache.K(row, column) = K(static_cast<int>(row), static_cast<int>(column));
        }
      }
      for (Eigen::Index row = 0; row < 4; ++row) {
        cache.D(row, 0) = D(static_cast<int>(row));
      }
      captured.snapshot.camera_caches.push_back(std::move(cache));
    }

    std::uint64_t covariance_dimension = 0;
    if (!eigen_index_to_u64(state->_Cov.rows(), covariance_dimension)) {
      return CP2CompositeStateStatus::kInvalidShape;
    }
    if (state->_variables.size() != active_expected) {
      return CP2CompositeStateStatus::kInvalidPointerGraph;
    }
    captured.snapshot.active_types.reserve(state->_variables.size());
    captured.snapshot.semantic_blocks.reserve(semantic_capacity);

    std::uint64_t expected_offset = 0;
    std::vector<bool> seen_clones(graph->clones.size(), false);
    for (const auto &variable : state->_variables) {
      if (!variable || variable->id() < 0 || variable->size() <= 0) {
        return CP2CompositeStateStatus::kInvalidActivePartition;
      }
      std::uint64_t covariance_id = 0;
      std::uint64_t error_size = 0;
      std::uint64_t updated_offset = 0;
      if (!eigen_index_to_u64(variable->id(), covariance_id) ||
          !eigen_index_to_u64(variable->size(), error_size) ||
          covariance_id != expected_offset ||
          !checked_add_u64(expected_offset, error_size, updated_offset) ||
          updated_offset > covariance_dimension) {
        return CP2CompositeStateStatus::kInvalidActivePartition;
      }
      expected_offset = updated_offset;
      graph->variables.push_back(variable);

      CP2ActiveStateType active;
      active.covariance_id = covariance_id;
      active.error_size = error_size;
      active.nominal = variable->value();
      active.fej = variable->fej();

      if (variable.get() == state->_imu.get()) {
        if (!captured.snapshot.active_types.empty()) {
          return CP2CompositeStateStatus::kInvalidPointerGraph;
        }
        active.tag = CP2ActiveStateTypeTag::kImu;
        append_imu_semantic_blocks(captured.snapshot.semantic_blocks, *state->_imu);
      } else {
        std::size_t clone_match = graph->clones.size();
        for (std::size_t index = 0; index < graph->clones.size(); ++index) {
          if (graph->clones[index].pose.get() == variable.get()) {
            if (clone_match != graph->clones.size()) {
              return CP2CompositeStateStatus::kInvalidPointerGraph;
            }
            clone_match = index;
          }
        }
        if (clone_match != graph->clones.size()) {
          if (seen_clones[clone_match]) {
            return CP2CompositeStateStatus::kInvalidPointerGraph;
          }
          seen_clones[clone_match] = true;
          const auto &clone = graph->clones[clone_match];
          active.tag = CP2ActiveStateTypeTag::kClone;
          active.clone_timestamp = clone.timestamp;
          append_clone_semantic_blocks(captured.snapshot.semantic_blocks, *clone.pose,
                                       clone.timestamp);
        } else {
          std::shared_ptr<ov_type::Landmark> matched_landmark;
          std::uint64_t matched_feature_id = 0;
          for (const auto &entry : state->_features_SLAM) {
            if (entry.second.get() == variable.get()) {
              if (matched_landmark) {
                return CP2CompositeStateStatus::kInvalidPointerGraph;
              }
              matched_landmark = entry.second;
              if (!size_to_u64(entry.first, matched_feature_id)) {
                return CP2CompositeStateStatus::kInvalidIdentity;
              }
            }
          }
          if (!matched_landmark) {
            return CP2CompositeStateStatus::kInvalidActivePartition;
          }
          active.tag = CP2ActiveStateTypeTag::kSlamLandmark;
          active.landmark = landmark_identity(matched_feature_id, *matched_landmark);

          CP2SemanticStateBlock block;
          block.kind = CP2SemanticStateKind::kSlamLandmark;
          block.covariance_id = covariance_id;
          block.error_size = error_size;
          block.landmark = active.landmark;
          captured.snapshot.semantic_blocks.push_back(std::move(block));

          CP2CompositePointerGraphData::LandmarkPointers pointers;
          pointers.feature_id = matched_feature_id;
          pointers.landmark = matched_landmark;
          graph->landmarks.push_back(std::move(pointers));
        }
      }
      captured.snapshot.active_types.push_back(std::move(active));
    }
    if (expected_offset != covariance_dimension ||
        std::find(seen_clones.begin(), seen_clones.end(), false) != seen_clones.end() ||
        graph->landmarks.size() != state->_features_SLAM.size()) {
      return CP2CompositeStateStatus::kInvalidActivePartition;
    }

    const CP2FixedImuCalibrationTag calibration_tags[] = {
        CP2FixedImuCalibrationTag::kDw, CP2FixedImuCalibrationTag::kDa,
        CP2FixedImuCalibrationTag::kTg, CP2FixedImuCalibrationTag::kGyroToImu,
        CP2FixedImuCalibrationTag::kAccelToImu};
    captured.snapshot.fixed_imu_calibrations.reserve(5U);
    for (std::size_t index = 0; index < 5U; ++index) {
      CP2FixedImuCalibration calibration;
      calibration.tag = calibration_tags[index];
      calibration.nominal = fixed_types[index]->value();
      calibration.fej = fixed_types[index]->fej();
      captured.snapshot.fixed_imu_calibrations.push_back(std::move(calibration));
    }
    captured.snapshot.time_offset_nominal = state->_calib_dt_CAMtoIMU->value();
    captured.snapshot.time_offset_fej = state->_calib_dt_CAMtoIMU->fej();

    // Only phase 0 is an admissible identity authority. Phase 1 uses the same
    // capture adapter for canonical values, but can never mint a replacement
    // token that would conceal a precommit object substitution.
    if (phase == CP2StatePhase::kPhase0Prior) {
      graph->captured_snapshot = captured.snapshot;
      captured.pointer_graph.data_ = std::move(graph);
    }
    output = std::move(captured);
    return CP2CompositeStateStatus::kAccepted;
  } catch (...) {
    return CP2CompositeStateStatus::kIncompleteStructure;
  }
}

bool CP2CompositeStateAdapter::PointerGraphMatches(
    const std::shared_ptr<State> &state,
    const CP2CompositePointerGraphToken &token) noexcept {
  if (!state || !token.data_ ||
      token.data_->capture_phase != CP2StatePhase::kPhase0Prior) {
    return false;
  }
  try {
    const CP2CompositePointerGraphData &graph = *token.data_;
    if (state->_variables.size() != graph.variables.size() || state->_imu.get() != graph.imu.get() ||
        !state->_imu || state->_imu->pose().get() != graph.imu_pose.get() ||
        state->_imu->q().get() != graph.imu_quaternion.get() ||
        state->_imu->p().get() != graph.imu_position.get() ||
        state->_imu->v().get() != graph.imu_velocity.get() ||
        state->_imu->bg().get() != graph.imu_gyro_bias.get() ||
        state->_imu->ba().get() != graph.imu_accel_bias.get()) {
      return false;
    }
    for (std::size_t index = 0; index < graph.variables.size(); ++index) {
      if (state->_variables[index].get() != graph.variables[index].get()) {
        return false;
      }
    }

    if (state->_clones_IMU.size() != graph.clones.size()) {
      return false;
    }
    std::size_t clone_index = 0;
    for (const auto &entry : state->_clones_IMU) {
      const CP2CompositePointerGraphData::ClonePointers &clone = graph.clones[clone_index++];
      if (!bit_equal(entry.first, clone.timestamp) || entry.second.get() != clone.pose.get() ||
          !entry.second || entry.second->q().get() != clone.quaternion.get() ||
          entry.second->p().get() != clone.position.get()) {
        return false;
      }
    }

    if (state->_features_SLAM.size() != graph.landmarks.size()) {
      return false;
    }
    for (const CP2CompositePointerGraphData::LandmarkPointers &landmark : graph.landmarks) {
      if (landmark.feature_id > static_cast<std::uint64_t>(std::numeric_limits<std::size_t>::max())) {
        return false;
      }
      const std::size_t feature_id = static_cast<std::size_t>(landmark.feature_id);
      const auto found = state->_features_SLAM.find(feature_id);
      if (found == state->_features_SLAM.end() || found->second.get() != landmark.landmark.get()) {
        return false;
      }
    }

    if (state->_calib_imu_dw.get() != graph.imu_dw.get() ||
        state->_calib_imu_da.get() != graph.imu_da.get() ||
        state->_calib_imu_tg.get() != graph.imu_tg.get() ||
        state->_calib_imu_GYROtoIMU.get() != graph.gyro_to_imu.get() ||
        state->_calib_imu_ACCtoIMU.get() != graph.accel_to_imu.get() ||
        state->_calib_dt_CAMtoIMU.get() != graph.camera_time_offset.get() ||
        state->_calib_IMUtoCAM.size() != graph.cameras.size() ||
        state->_cam_intrinsics.size() != graph.cameras.size() ||
        state->_cam_intrinsics_cameras.size() != graph.cameras.size()) {
      return false;
    }
    for (const CP2CompositePointerGraphData::CameraPointers &camera : graph.cameras) {
      if (camera.camera_id > static_cast<std::uint64_t>(std::numeric_limits<std::size_t>::max())) {
        return false;
      }
      const std::size_t camera_id = static_cast<std::size_t>(camera.camera_id);
      const auto extrinsic = state->_calib_IMUtoCAM.find(camera_id);
      const auto intrinsic = state->_cam_intrinsics.find(camera_id);
      const auto cache = state->_cam_intrinsics_cameras.find(camera_id);
      if (extrinsic == state->_calib_IMUtoCAM.end() || intrinsic == state->_cam_intrinsics.end() ||
          cache == state->_cam_intrinsics_cameras.end() ||
          extrinsic->second.get() != camera.extrinsic.get() ||
          intrinsic->second.get() != camera.intrinsic.get() || cache->second.get() != camera.cache.get()) {
        return false;
      }
    }
    return true;
  } catch (...) {
    return false;
  }
}

CP2CompositeStateStatus CP2CompositeStateAdapter::ProjectPreview(
    const CP2CompositeStateSnapshot &snapshot, MSCKFUpdatePreviewSnapshot &output) {
  if (snapshot.phase != CP2StatePhase::kPhase0Prior) {
    return CP2CompositeStateStatus::kInvalidPhase;
  }
  const CP2CompositeStateStatus validity = Validate(snapshot);
  if (validity != CP2CompositeStateStatus::kAccepted) {
    return validity;
  }
  try {
    MSCKFUpdatePreviewSnapshot projected;
    projected.covariance = snapshot.covariance;
    projected.state_blocks.reserve(snapshot.active_types.size());
    for (const CP2ActiveStateType &active : snapshot.active_types) {
      Eigen::Index covariance_id = 0;
      Eigen::Index error_size = 0;
      if (!u64_to_eigen_index(active.covariance_id, covariance_id) ||
          !u64_to_eigen_index(active.error_size, error_size)) {
        return CP2CompositeStateStatus::kInvalidShape;
      }
      projected.state_blocks.push_back({covariance_id, error_size, covariance_id});
    }
    output = std::move(projected);
    return CP2CompositeStateStatus::kAccepted;
  } catch (...) {
    return CP2CompositeStateStatus::kIncompleteStructure;
  }
}

CP2CompositeStateStatus CP2CompositeStateAdapter::BuildExpected(
    const CP2CompositeStateSnapshot &phase0,
    const MSCKFUpdatePreviewResult &accepted_baseline_proposal,
    CP2CompositeStateSnapshot &output,
    std::uint64_t &type_update_calls) {
  type_update_calls = 0;
  if (phase0.phase != CP2StatePhase::kPhase0Prior) {
    return CP2CompositeStateStatus::kInvalidPhase;
  }
  const CP2CompositeStateStatus prior_validity = Validate(phase0);
  if (prior_validity != CP2CompositeStateStatus::kAccepted) {
    return prior_validity;
  }
  const Eigen::VectorXd &dx = accepted_baseline_proposal.dx;
  const Eigen::MatrixXd &P_plus = accepted_baseline_proposal.P_plus;
  const MSCKFUpdatePreviewDiagnostics &diagnostics =
      accepted_baseline_proposal.diagnostics;
  if (!accepted_baseline_proposal.accepted() ||
      diagnostics.stage != MSCKFUpdatePreviewStage::kAccepted ||
      diagnostics.state_dimension != phase0.covariance.rows() ||
      diagnostics.jitter_count != 0U || diagnostics.repair_count != 0U ||
      diagnostics.alternate_solve_count != 0U ||
      diagnostics.clamp_count != 0U ||
      diagnostics.regularization_count != 0U ||
      diagnostics.fallback_count != 0U ||
      dx.rows() != phase0.covariance.rows() || dx.cols() != 1 ||
      P_plus.rows() != phase0.covariance.rows() ||
      P_plus.cols() != phase0.covariance.cols() || !matrix_finite(dx) ||
      !matrix_finite(P_plus)) {
    return CP2CompositeStateStatus::kInvalidProposal;
  }

  try {
    CP2CompositeStateSnapshot expected = phase0;
    expected.phase = CP2StatePhase::kPhase2ExpectedPostcommit;
    expected.covariance = P_plus;
    std::uint64_t completed_calls = 0;

    for (std::size_t index = 0; index < phase0.active_types.size(); ++index) {
      const CP2ActiveStateType &active = phase0.active_types[index];
      std::unique_ptr<ov_type::Type> detached;
      switch (active.tag) {
      case CP2ActiveStateTypeTag::kImu:
        detached.reset(new ov_type::IMU());
        break;
      case CP2ActiveStateTypeTag::kClone:
        detached.reset(new ov_type::PoseJPL());
        break;
      case CP2ActiveStateTypeTag::kSlamLandmark:
        detached.reset(new ov_type::Vec(static_cast<int>(active.error_size)));
        break;
      default:
        return CP2CompositeStateStatus::kInvalidProposal;
      }

      if (!detached || active.covariance_id >
                           static_cast<std::uint64_t>(std::numeric_limits<int>::max()) ||
          active.error_size > static_cast<std::uint64_t>(std::numeric_limits<int>::max())) {
        return CP2CompositeStateStatus::kInvalidProposal;
      }
      const int covariance_id = static_cast<int>(active.covariance_id);
      const int error_size = static_cast<int>(active.error_size);
      detached->set_value(active.nominal);
      detached->set_fej(active.fej);
      detached->set_local_id(covariance_id);
      detached->update(dx.segment(covariance_id, error_size));
      if (completed_calls == std::numeric_limits<std::uint64_t>::max()) {
        return CP2CompositeStateStatus::kInvalidProposal;
      }
      ++completed_calls;
      expected.active_types[index].nominal = detached->value();
      expected.active_types[index].fej = active.fej;
    }

    const CP2CompositeStateStatus expected_validity = Validate(expected);
    if (expected_validity != CP2CompositeStateStatus::kAccepted) {
      return expected_validity;
    }
    output = std::move(expected);
    type_update_calls = completed_calls;
    return CP2CompositeStateStatus::kAccepted;
  } catch (...) {
    return CP2CompositeStateStatus::kInvalidProposal;
  }
}

CP2CompositeStateStatus CP2CompositeStateAdapter::PreparePostcommit(
    const CP2CompositeStateCapture &phase0,
    CP2PreparedPostcommitCapture &output) {
  if (output.lifecycle() != CP2PreparedPostcommitState::kEmpty) {
    return CP2CompositeStateStatus::kNotPrepared;
  }
  if (phase0.snapshot.phase != CP2StatePhase::kPhase0Prior ||
      !phase0.pointer_graph.data_ ||
      phase0.pointer_graph.data_->capture_phase !=
          CP2StatePhase::kPhase0Prior) {
    return CP2CompositeStateStatus::kInvalidPhase;
  }
  if (!CanonicallyEqual(phase0.snapshot,
                        phase0.pointer_graph.data_->captured_snapshot)) {
    return CP2CompositeStateStatus::kInvalidPointerGraph;
  }
  const CP2CompositeStateStatus validity = Validate(phase0.snapshot);
  if (validity != CP2CompositeStateStatus::kAccepted) {
    return validity;
  }
  try {
    std::unique_ptr<CP2CompositeStateSnapshot> snapshot =
        std::unique_ptr<CP2CompositeStateSnapshot>(
            new CP2CompositeStateSnapshot(phase0.snapshot));
    snapshot->phase = CP2StatePhase::kPhase3LivePostcommit;

    // Touch every destination coefficient while allocation is still legal.
    // FillPostcommitNoAlloc subsequently overwrites every canonical field from
    // live state; none of these phase-0 values is used as phase-3 evidence.
    snapshot->covariance.setZero();
    for (CP2ActiveStateType &active : snapshot->active_types) {
      active.nominal.setZero();
      active.fej.setZero();
    }
    for (CP2FixedImuCalibration &fixed : snapshot->fixed_imu_calibrations) {
      fixed.nominal.setZero();
      fixed.fej.setZero();
    }
    snapshot->time_offset_nominal.setZero();
    snapshot->time_offset_fej.setZero();
    for (CP2CameraFixedCalibration &camera : snapshot->camera_calibrations) {
      camera.extrinsic_nominal.setZero();
      camera.extrinsic_fej.setZero();
      camera.intrinsic_nominal.setZero();
      camera.intrinsic_fej.setZero();
    }
    for (CP2CameraModelCache &cache : snapshot->camera_caches) {
      cache.calibration.setZero();
      cache.K.setZero();
      cache.D.setZero();
    }
    bool unprepared = false;
    if (!phase0.pointer_graph.data_->postcommit_prepared.compare_exchange_strong(
            unprepared, true, std::memory_order_acq_rel,
            std::memory_order_acquire)) {
      return CP2CompositeStateStatus::kNotPrepared;
    }
    output.pointer_graph_ = phase0.pointer_graph;
    output.snapshot_ = std::move(snapshot);
    output.state_ = CP2PreparedPostcommitState::kPrepared;
    return CP2CompositeStateStatus::kAccepted;
  } catch (...) {
    return CP2CompositeStateStatus::kIncompleteStructure;
  }
}

CP2CompositeStateStatus CP2CompositeStateAdapter::FillPostcommitNoAlloc(
    const std::shared_ptr<State> &state,
    CP2PreparedPostcommitCapture &prepared) noexcept {
  if (!prepared.prepared() ||
      prepared.snapshot_->phase != CP2StatePhase::kPhase3LivePostcommit) {
    return CP2CompositeStateStatus::kNotPrepared;
  }

  // The transition is one-way. A failed or interrupted fill can never be
  // retried or mistaken for a publishable complete value.
  prepared.state_ = CP2PreparedPostcommitState::kFailed;
  if (!state) {
    return CP2CompositeStateStatus::kNullState;
  }
  try {
    if (!PointerGraphMatches(state, prepared.pointer_graph_) ||
        !prepared.pointer_graph_.data_) {
      return CP2CompositeStateStatus::kInvalidPointerGraph;
    }
    const CP2CompositePointerGraphData &graph = *prepared.pointer_graph_.data_;
    CP2CompositeStateSnapshot &snapshot = *prepared.snapshot_;

    std::size_t twice_clone_count = 0;
    std::size_t expected_semantic_count = 0;
    if (!checked_multiply_size(2U, graph.clones.size(), twice_clone_count) ||
        !checked_add_size(5U, twice_clone_count, expected_semantic_count) ||
        !checked_add_size(expected_semantic_count, graph.landmarks.size(),
                          expected_semantic_count)) {
      return CP2CompositeStateStatus::kInvalidShape;
    }

    if (snapshot.covariance.rows() != state->_Cov.rows() ||
        snapshot.covariance.cols() != state->_Cov.cols() ||
        snapshot.active_types.size() != state->_variables.size() ||
        snapshot.fixed_imu_calibrations.size() != 5U ||
        snapshot.camera_calibrations.size() != graph.cameras.size() ||
        snapshot.camera_caches.size() != graph.cameras.size() ||
        snapshot.semantic_blocks.size() != expected_semantic_count ||
        !imu_parent_consistent(state->_imu)) {
      return CP2CompositeStateStatus::kInvalidShape;
    }
    if (!copy_matrix_noalloc(state->_Cov, snapshot.covariance)) {
      return CP2CompositeStateStatus::kInvalidShape;
    }
    snapshot.phase = CP2StatePhase::kPhase3LivePostcommit;
    snapshot.timestamp = state->_timestamp;

    std::size_t semantic_index = 0;
    std::uint64_t expected_offset = 0;
    for (std::size_t index = 0; index < graph.variables.size(); ++index) {
      const std::shared_ptr<ov_type::Type> &variable = graph.variables[index];
      CP2ActiveStateType &destination = snapshot.active_types[index];
      if (!variable || variable->id() < 0 || variable->size() <= 0) {
        return CP2CompositeStateStatus::kInvalidActivePartition;
      }
      std::uint64_t covariance_id = 0;
      std::uint64_t error_size = 0;
      std::uint64_t updated_offset = 0;
      if (!eigen_index_to_u64(variable->id(), covariance_id) ||
          !eigen_index_to_u64(variable->size(), error_size) || covariance_id != expected_offset ||
          !checked_add_u64(expected_offset, error_size, updated_offset) ||
          updated_offset >
              static_cast<std::uint64_t>(snapshot.covariance.rows()) ||
          !copy_matrix_noalloc(variable->value(), destination.nominal) ||
          !copy_matrix_noalloc(variable->fej(), destination.fej)) {
        return CP2CompositeStateStatus::kInvalidShape;
      }
      expected_offset = updated_offset;
      destination.covariance_id = covariance_id;
      destination.error_size = error_size;
      destination.clone_timestamp = 0.0;
      destination.landmark = CP2LandmarkIdentity();

      if (variable.get() == graph.imu.get()) {
        if (index != 0 || semantic_index > snapshot.semantic_blocks.size() ||
            snapshot.semantic_blocks.size() - semantic_index < 5U) {
          return CP2CompositeStateStatus::kInvalidActivePartition;
        }
        destination.tag = CP2ActiveStateTypeTag::kImu;
        const CP2SemanticStateKind kinds[] = {
            CP2SemanticStateKind::kImuTheta, CP2SemanticStateKind::kImuPosition,
            CP2SemanticStateKind::kImuVelocity, CP2SemanticStateKind::kImuGyroBias,
            CP2SemanticStateKind::kImuAccelBias};
        ov_type::Type *subvariables[] = {
            state->_imu->q().get(), state->_imu->p().get(), state->_imu->v().get(),
            state->_imu->bg().get(), state->_imu->ba().get()};
        for (std::size_t sub_index = 0; sub_index < 5U; ++sub_index) {
          if (!subvariables[sub_index] || subvariables[sub_index]->id() < 0 ||
              subvariables[sub_index]->size() != 3) {
            return CP2CompositeStateStatus::kInvalidSemanticPartition;
          }
          CP2SemanticStateBlock &block = snapshot.semantic_blocks[semantic_index++];
          block = CP2SemanticStateBlock();
          block.kind = kinds[sub_index];
          block.covariance_id = static_cast<std::uint64_t>(subvariables[sub_index]->id());
          block.error_size = 3U;
        }
        continue;
      }

      std::size_t clone_index = graph.clones.size();
      for (std::size_t candidate = 0; candidate < graph.clones.size(); ++candidate) {
        if (graph.clones[candidate].pose.get() == variable.get()) {
          if (clone_index != graph.clones.size()) {
            return CP2CompositeStateStatus::kInvalidPointerGraph;
          }
          clone_index = candidate;
        }
      }
      if (clone_index != graph.clones.size()) {
        const CP2CompositePointerGraphData::ClonePointers &clone = graph.clones[clone_index];
        double live_clone_timestamp = 0.0;
        bool live_clone_found = false;
        for (const auto &entry : state->_clones_IMU) {
          if (entry.second.get() == variable.get()) {
            if (live_clone_found) {
              return CP2CompositeStateStatus::kInvalidPointerGraph;
            }
            live_clone_timestamp = entry.first;
            live_clone_found = true;
          }
        }
        if (!pose_parent_consistent(clone.pose) || !live_clone_found ||
            semantic_index > snapshot.semantic_blocks.size() ||
            snapshot.semantic_blocks.size() - semantic_index < 2U) {
          return CP2CompositeStateStatus::kInvalidSemanticPartition;
        }
        destination.tag = CP2ActiveStateTypeTag::kClone;
        destination.clone_timestamp = live_clone_timestamp;

        CP2SemanticStateBlock &theta = snapshot.semantic_blocks[semantic_index++];
        theta = CP2SemanticStateBlock();
        theta.kind = CP2SemanticStateKind::kCloneTheta;
        theta.covariance_id = static_cast<std::uint64_t>(clone.quaternion->id());
        theta.error_size = 3U;
        theta.clone_timestamp = live_clone_timestamp;

        CP2SemanticStateBlock &position = snapshot.semantic_blocks[semantic_index++];
        position = CP2SemanticStateBlock();
        position.kind = CP2SemanticStateKind::kClonePosition;
        position.covariance_id = static_cast<std::uint64_t>(clone.position->id());
        position.error_size = 3U;
        position.clone_timestamp = live_clone_timestamp;
        continue;
      }

      const CP2CompositePointerGraphData::LandmarkPointers *landmark = nullptr;
      for (const CP2CompositePointerGraphData::LandmarkPointers &candidate : graph.landmarks) {
        if (candidate.landmark.get() == variable.get()) {
          if (landmark != nullptr) {
            return CP2CompositeStateStatus::kInvalidPointerGraph;
          }
          landmark = &candidate;
        }
      }
      if (landmark == nullptr || semantic_index >= snapshot.semantic_blocks.size() ||
          landmark->feature_id >
              static_cast<std::uint64_t>(std::numeric_limits<std::size_t>::max())) {
        return CP2CompositeStateStatus::kInvalidPointerGraph;
      }
      std::size_t live_feature_key = 0;
      bool live_feature_found = false;
      for (const auto &entry : state->_features_SLAM) {
        if (entry.second.get() == variable.get()) {
          if (live_feature_found) {
            return CP2CompositeStateStatus::kInvalidPointerGraph;
          }
          live_feature_key = entry.first;
          live_feature_found = true;
        }
      }
      std::uint64_t live_feature_id = 0;
      if (!live_feature_found || !size_to_u64(live_feature_key, live_feature_id) ||
          landmark->landmark->_featid != live_feature_key) {
        return CP2CompositeStateStatus::kInvalidPointerGraph;
      }
      destination.tag = CP2ActiveStateTypeTag::kSlamLandmark;
      destination.landmark = landmark_identity(live_feature_id, *landmark->landmark);

      CP2SemanticStateBlock &block = snapshot.semantic_blocks[semantic_index++];
      block = CP2SemanticStateBlock();
      block.kind = CP2SemanticStateKind::kSlamLandmark;
      block.covariance_id = covariance_id;
      block.error_size = error_size;
      block.landmark = destination.landmark;
    }
    if (expected_offset != static_cast<std::uint64_t>(snapshot.covariance.rows()) ||
        semantic_index != snapshot.semantic_blocks.size()) {
      return CP2CompositeStateStatus::kInvalidSemanticPartition;
    }

    ov_type::Type *fixed_sources[] = {
        state->_calib_imu_dw.get(), state->_calib_imu_da.get(),
        state->_calib_imu_tg.get(), state->_calib_imu_GYROtoIMU.get(),
        state->_calib_imu_ACCtoIMU.get()};
    const CP2FixedImuCalibrationTag fixed_tags[] = {
        CP2FixedImuCalibrationTag::kDw, CP2FixedImuCalibrationTag::kDa,
        CP2FixedImuCalibrationTag::kTg, CP2FixedImuCalibrationTag::kGyroToImu,
        CP2FixedImuCalibrationTag::kAccelToImu};
    for (std::size_t index = 0; index < 5U; ++index) {
      CP2FixedImuCalibration &destination = snapshot.fixed_imu_calibrations[index];
      if (!fixed_sources[index] || fixed_sources[index]->id() != -1 ||
          !copy_matrix_noalloc(fixed_sources[index]->value(), destination.nominal) ||
          !copy_matrix_noalloc(fixed_sources[index]->fej(), destination.fej)) {
        return CP2CompositeStateStatus::kInvalidShape;
      }
      destination.tag = fixed_tags[index];
    }
    if (!state->_calib_dt_CAMtoIMU || state->_calib_dt_CAMtoIMU->id() != -1 ||
        !copy_matrix_noalloc(state->_calib_dt_CAMtoIMU->value(),
                             snapshot.time_offset_nominal) ||
        !copy_matrix_noalloc(state->_calib_dt_CAMtoIMU->fej(), snapshot.time_offset_fej)) {
      return CP2CompositeStateStatus::kInvalidShape;
    }

    for (std::size_t index = 0; index < graph.cameras.size(); ++index) {
      const CP2CompositePointerGraphData::CameraPointers &source = graph.cameras[index];
      CP2CameraFixedCalibration &calibration = snapshot.camera_calibrations[index];
      CP2CameraModelCache &cache = snapshot.camera_caches[index];
      std::size_t live_camera_key = 0;
      bool live_camera_found = false;
      for (const auto &entry : state->_calib_IMUtoCAM) {
        if (entry.second.get() == source.extrinsic.get()) {
          if (live_camera_found) {
            return CP2CompositeStateStatus::kInvalidPointerGraph;
          }
          live_camera_key = entry.first;
          live_camera_found = true;
        }
      }
      const auto live_intrinsic = state->_cam_intrinsics.find(live_camera_key);
      const auto live_cache = state->_cam_intrinsics_cameras.find(live_camera_key);
      std::uint64_t live_camera_id = 0;
      if (!source.extrinsic || !source.intrinsic || !source.cache ||
          !live_camera_found || !size_to_u64(live_camera_key, live_camera_id) ||
          live_intrinsic == state->_cam_intrinsics.end() ||
          live_cache == state->_cam_intrinsics_cameras.end() ||
          live_intrinsic->second.get() != source.intrinsic.get() ||
          live_cache->second.get() != source.cache.get() ||
          typeid(*source.cache) != typeid(ov_core::CamRadtan) ||
          source.extrinsic->id() != -1 || source.intrinsic->id() != -1 ||
          !copy_matrix_noalloc(source.extrinsic->value(), calibration.extrinsic_nominal) ||
          !copy_matrix_noalloc(source.extrinsic->fej(), calibration.extrinsic_fej) ||
          !copy_matrix_noalloc(source.intrinsic->value(), calibration.intrinsic_nominal) ||
          !copy_matrix_noalloc(source.intrinsic->fej(), calibration.intrinsic_fej) ||
          !copy_matrix_noalloc(source.cache->cp2_cache_value(), cache.calibration) ||
          cache.K.rows() != 3 || cache.K.cols() != 3 || cache.D.rows() != 4 ||
          cache.D.cols() != 1) {
        return CP2CompositeStateStatus::kInvalidShape;
      }
      calibration.camera_id = live_camera_id;
      cache.camera_id = live_camera_id;
      cache.width = static_cast<std::int64_t>(source.cache->cp2_cache_width());
      cache.height = static_cast<std::int64_t>(source.cache->cp2_cache_height());
      const cv::Matx33d &K = source.cache->cp2_cache_K();
      const cv::Vec4d &D = source.cache->cp2_cache_D();
      for (Eigen::Index row = 0; row < 3; ++row) {
        for (Eigen::Index column = 0; column < 3; ++column) {
          cache.K.coeffRef(row, column) = K(static_cast<int>(row), static_cast<int>(column));
        }
      }
      for (Eigen::Index row = 0; row < 4; ++row) {
        cache.D.coeffRef(row, 0) = D(static_cast<int>(row));
      }
    }

    prepared.state_ = CP2PreparedPostcommitState::kComplete;
    return CP2CompositeStateStatus::kAccepted;
  } catch (...) {
    return CP2CompositeStateStatus::kIncompleteStructure;
  }
}

CP2CompositeStateStatus CP2CompositeStateAdapter::HandoffPostcommitNoAlloc(
    CP2PreparedPostcommitCapture &prepared,
    std::unique_ptr<CP2CompositeStateSnapshot> &output) noexcept {
  static_assert(
      std::is_nothrow_move_assignable<
          std::unique_ptr<CP2CompositeStateSnapshot>>::value,
      "phase-3 owning handoff requires noexcept unique_ptr move assignment");
  if (!prepared.complete()) {
    return CP2CompositeStateStatus::kNotPrepared;
  }
  // Handoff, like fill, is a single attempt. Any rejected destination leaves
  // the owning value private and permanently nonpublishable.
  prepared.state_ = CP2PreparedPostcommitState::kFailed;
  if (output) {
    return CP2CompositeStateStatus::kInvalidProposal;
  }
  output = std::move(prepared.snapshot_);
  prepared.state_ = CP2PreparedPostcommitState::kHandedOff;
  return CP2CompositeStateStatus::kAccepted;
}

#if defined(OV_MSCKF_CP2_TESTING)
void CP2CompositeStateAdapter::TestInvalidatePreparedStorage(
    CP2PreparedPostcommitCapture &prepared) noexcept {
  prepared.snapshot_.reset();
  prepared.state_ = CP2PreparedPostcommitState::kFailed;
}

void CP2CompositeStateAdapter::TestInvalidatePreparedPointerToken(
    CP2PreparedPostcommitCapture &prepared) noexcept {
  prepared.pointer_graph_.data_.reset();
}
#endif

} // namespace ov_msckf
