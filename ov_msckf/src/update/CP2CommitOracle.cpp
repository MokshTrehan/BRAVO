/*
 * SchurVIO-Lite CP2 exact live-commit oracle and counter boundary.
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include "CP2CommitOracle.h"

#include <cmath>
#include <cstddef>
#include <cstring>
#include <limits>
#include <type_traits>

namespace ov_msckf {

namespace {

static_assert(sizeof(double) == sizeof(std::uint64_t),
              "CP2 requires binary64");
static_assert(std::numeric_limits<double>::is_iec559,
              "CP2 requires IEEE-754 binary64");

std::uint64_t double_bits(double value) noexcept {
  std::uint64_t bits = 0;
  std::memcpy(&bits, &value, sizeof(bits));
  return bits;
}

bool bit_equal(double left, double right) noexcept {
  return double_bits(left) == double_bits(right);
}

bool size_to_u64(std::size_t value, std::uint64_t &output) noexcept {
  if (std::numeric_limits<std::size_t>::digits >
          std::numeric_limits<std::uint64_t>::digits &&
      value >
          static_cast<std::size_t>(std::numeric_limits<std::uint64_t>::max())) {
    return false;
  }
  output = static_cast<std::uint64_t>(value);
  return true;
}

template <typename Derived>
bool matrix_finite(const Eigen::MatrixBase<Derived> &matrix) noexcept {
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
      if (!bit_equal(left.derived().coeff(row, column),
                     right.derived().coeff(row, column))) {
        return false;
      }
    }
  }
  return true;
}

template <typename Derived>
bool matrix_population(const Eigen::MatrixBase<Derived> &matrix,
                       std::uint64_t &population) noexcept {
  std::uint64_t rows = 0;
  std::uint64_t columns = 0;
  return cp2_checked_eigen_index_to_u64(matrix.rows(), rows) &&
         cp2_checked_eigen_index_to_u64(matrix.cols(), columns) &&
         cp2_checked_multiply_u64(rows, columns, population);
}

template <typename Left, typename Right>
bool matrix_mismatch_count(const Eigen::MatrixBase<Left> &left,
                           const Eigen::MatrixBase<Right> &right,
                           std::uint64_t &population,
                           std::uint64_t &mismatches) noexcept {
  if (left.rows() != right.rows() || left.cols() != right.cols() ||
      !matrix_population(left, population)) {
    return false;
  }
  std::uint64_t count = 0;
  for (Eigen::Index row = 0; row < left.rows(); ++row) {
    for (Eigen::Index column = 0; column < left.cols(); ++column) {
      if (!bit_equal(left.derived().coeff(row, column),
                     right.derived().coeff(row, column))) {
        if (!cp2_checked_add_u64(count, UINT64_C(1), count)) {
          return false;
        }
      }
    }
  }
  mismatches = count;
  return true;
}

bool landmark_identity_equal(const CP2LandmarkIdentity &left,
                             const CP2LandmarkIdentity &right) noexcept {
  return left.feature_id == right.feature_id &&
         left.representation == right.representation &&
         left.anchor_camera_id == right.anchor_camera_id &&
         bit_equal(left.anchor_timestamp, right.anchor_timestamp);
}

bool semantic_structure_equal(const CP2SemanticStateBlock &left,
                              const CP2SemanticStateBlock &right) noexcept {
  if (left.kind != right.kind ||
      left.covariance_id != right.covariance_id ||
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

bool active_structure_equal(const CP2ActiveStateType &left,
                            const CP2ActiveStateType &right) noexcept {
  if (left.tag != right.tag || left.covariance_id != right.covariance_id ||
      left.error_size != right.error_size ||
      left.nominal.rows() != right.nominal.rows() ||
      left.nominal.cols() != right.nominal.cols() ||
      left.fej.rows() != right.fej.rows() ||
      left.fej.cols() != right.fej.cols()) {
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

bool matrix_shape_equal(const Eigen::MatrixXd &left,
                        const Eigen::MatrixXd &right) noexcept {
  return left.rows() == right.rows() && left.cols() == right.cols();
}

bool snapshot_structure_equal(const CP2CompositeStateSnapshot &left,
                              const CP2CompositeStateSnapshot &right) noexcept {
  if (!matrix_shape_equal(left.covariance, right.covariance) ||
      left.semantic_blocks.size() != right.semantic_blocks.size() ||
      left.active_types.size() != right.active_types.size() ||
      left.fixed_imu_calibrations.size() !=
          right.fixed_imu_calibrations.size() ||
      !matrix_shape_equal(left.time_offset_nominal,
                          right.time_offset_nominal) ||
      !matrix_shape_equal(left.time_offset_fej, right.time_offset_fej) ||
      left.camera_calibrations.size() != right.camera_calibrations.size() ||
      left.camera_caches.size() != right.camera_caches.size()) {
    return false;
  }

  for (std::size_t index = 0; index < left.semantic_blocks.size(); ++index) {
    if (!semantic_structure_equal(left.semantic_blocks[index],
                                  right.semantic_blocks[index])) {
      return false;
    }
  }
  for (std::size_t index = 0; index < left.active_types.size(); ++index) {
    if (!active_structure_equal(left.active_types[index],
                                right.active_types[index])) {
      return false;
    }
  }
  for (std::size_t index = 0; index < left.fixed_imu_calibrations.size();
       ++index) {
    const CP2FixedImuCalibration &lhs = left.fixed_imu_calibrations[index];
    const CP2FixedImuCalibration &rhs = right.fixed_imu_calibrations[index];
    if (lhs.tag != rhs.tag || !matrix_shape_equal(lhs.nominal, rhs.nominal) ||
        !matrix_shape_equal(lhs.fej, rhs.fej)) {
      return false;
    }
  }
  for (std::size_t index = 0; index < left.camera_calibrations.size();
       ++index) {
    const CP2CameraFixedCalibration &lhs = left.camera_calibrations[index];
    const CP2CameraFixedCalibration &rhs = right.camera_calibrations[index];
    if (lhs.camera_id != rhs.camera_id ||
        !matrix_shape_equal(lhs.extrinsic_nominal, rhs.extrinsic_nominal) ||
        !matrix_shape_equal(lhs.extrinsic_fej, rhs.extrinsic_fej) ||
        !matrix_shape_equal(lhs.intrinsic_nominal, rhs.intrinsic_nominal) ||
        !matrix_shape_equal(lhs.intrinsic_fej, rhs.intrinsic_fej)) {
      return false;
    }
  }
  for (std::size_t index = 0; index < left.camera_caches.size(); ++index) {
    const CP2CameraModelCache &lhs = left.camera_caches[index];
    const CP2CameraModelCache &rhs = right.camera_caches[index];
    if (lhs.camera_id != rhs.camera_id ||
        !matrix_shape_equal(lhs.calibration, rhs.calibration) ||
        !matrix_shape_equal(lhs.K, rhs.K) ||
        !matrix_shape_equal(lhs.D, rhs.D)) {
      return false;
    }
  }
  return true;
}

bool snapshot_finite(const CP2CompositeStateSnapshot &snapshot) noexcept {
  if (!std::isfinite(snapshot.timestamp) ||
      !matrix_finite(snapshot.covariance) ||
      !matrix_finite(snapshot.time_offset_nominal) ||
      !matrix_finite(snapshot.time_offset_fej)) {
    return false;
  }
  for (const CP2SemanticStateBlock &block : snapshot.semantic_blocks) {
    if ((block.kind == CP2SemanticStateKind::kCloneTheta ||
         block.kind == CP2SemanticStateKind::kClonePosition) &&
        !std::isfinite(block.clone_timestamp)) {
      return false;
    }
    if (block.kind == CP2SemanticStateKind::kSlamLandmark &&
        !std::isfinite(block.landmark.anchor_timestamp)) {
      return false;
    }
  }
  for (const CP2ActiveStateType &active : snapshot.active_types) {
    if (!matrix_finite(active.nominal) || !matrix_finite(active.fej) ||
        (active.tag == CP2ActiveStateTypeTag::kClone &&
         !std::isfinite(active.clone_timestamp)) ||
        (active.tag == CP2ActiveStateTypeTag::kSlamLandmark &&
         !std::isfinite(active.landmark.anchor_timestamp))) {
      return false;
    }
  }
  for (const CP2FixedImuCalibration &fixed :
       snapshot.fixed_imu_calibrations) {
    if (!matrix_finite(fixed.nominal) || !matrix_finite(fixed.fej)) {
      return false;
    }
  }
  for (const CP2CameraFixedCalibration &camera :
       snapshot.camera_calibrations) {
    if (!matrix_finite(camera.extrinsic_nominal) ||
        !matrix_finite(camera.extrinsic_fej) ||
        !matrix_finite(camera.intrinsic_nominal) ||
        !matrix_finite(camera.intrinsic_fej)) {
      return false;
    }
  }
  for (const CP2CameraModelCache &cache : snapshot.camera_caches) {
    if (!matrix_finite(cache.calibration) || !matrix_finite(cache.K) ||
        !matrix_finite(cache.D)) {
      return false;
    }
  }
  return true;
}

bool snapshot_payload_equal(const CP2CompositeStateSnapshot &left,
                            const CP2CompositeStateSnapshot &right) noexcept {
  if (!snapshot_structure_equal(left, right) ||
      !bit_equal(left.timestamp, right.timestamp) ||
      !matrix_bit_equal(left.covariance, right.covariance)) {
    return false;
  }
  for (std::size_t index = 0; index < left.active_types.size(); ++index) {
    if (!matrix_bit_equal(left.active_types[index].nominal,
                          right.active_types[index].nominal) ||
        !matrix_bit_equal(left.active_types[index].fej,
                          right.active_types[index].fej)) {
      return false;
    }
  }
  for (std::size_t index = 0; index < left.fixed_imu_calibrations.size();
       ++index) {
    if (!matrix_bit_equal(left.fixed_imu_calibrations[index].nominal,
                          right.fixed_imu_calibrations[index].nominal) ||
        !matrix_bit_equal(left.fixed_imu_calibrations[index].fej,
                          right.fixed_imu_calibrations[index].fej)) {
      return false;
    }
  }
  if (!matrix_bit_equal(left.time_offset_nominal,
                        right.time_offset_nominal) ||
      !matrix_bit_equal(left.time_offset_fej, right.time_offset_fej)) {
    return false;
  }
  for (std::size_t index = 0; index < left.camera_calibrations.size();
       ++index) {
    const CP2CameraFixedCalibration &lhs = left.camera_calibrations[index];
    const CP2CameraFixedCalibration &rhs = right.camera_calibrations[index];
    if (!matrix_bit_equal(lhs.extrinsic_nominal, rhs.extrinsic_nominal) ||
        !matrix_bit_equal(lhs.extrinsic_fej, rhs.extrinsic_fej) ||
        !matrix_bit_equal(lhs.intrinsic_nominal, rhs.intrinsic_nominal) ||
        !matrix_bit_equal(lhs.intrinsic_fej, rhs.intrinsic_fej)) {
      return false;
    }
  }
  for (std::size_t index = 0; index < left.camera_caches.size(); ++index) {
    const CP2CameraModelCache &lhs = left.camera_caches[index];
    const CP2CameraModelCache &rhs = right.camera_caches[index];
    if (lhs.width != rhs.width || lhs.height != rhs.height ||
        !matrix_bit_equal(lhs.calibration, rhs.calibration) ||
        !matrix_bit_equal(lhs.K, rhs.K) || !matrix_bit_equal(lhs.D, rhs.D)) {
      return false;
    }
  }
  return true;
}

bool immutable_prior_fields_equal(
    const CP2CompositeStateSnapshot &phase0,
    const CP2CompositeStateSnapshot &phase2) noexcept {
  if (!snapshot_structure_equal(phase0, phase2) ||
      !bit_equal(phase0.timestamp, phase2.timestamp)) {
    return false;
  }
  for (std::size_t index = 0; index < phase0.active_types.size(); ++index) {
    if (!matrix_bit_equal(phase0.active_types[index].fej,
                          phase2.active_types[index].fej)) {
      return false;
    }
  }
  for (std::size_t index = 0; index < phase0.fixed_imu_calibrations.size();
       ++index) {
    if (!matrix_bit_equal(phase0.fixed_imu_calibrations[index].nominal,
                          phase2.fixed_imu_calibrations[index].nominal) ||
        !matrix_bit_equal(phase0.fixed_imu_calibrations[index].fej,
                          phase2.fixed_imu_calibrations[index].fej)) {
      return false;
    }
  }
  if (!matrix_bit_equal(phase0.time_offset_nominal,
                        phase2.time_offset_nominal) ||
      !matrix_bit_equal(phase0.time_offset_fej, phase2.time_offset_fej)) {
    return false;
  }
  for (std::size_t index = 0; index < phase0.camera_calibrations.size();
       ++index) {
    const CP2CameraFixedCalibration &lhs = phase0.camera_calibrations[index];
    const CP2CameraFixedCalibration &rhs = phase2.camera_calibrations[index];
    if (!matrix_bit_equal(lhs.extrinsic_nominal, rhs.extrinsic_nominal) ||
        !matrix_bit_equal(lhs.extrinsic_fej, rhs.extrinsic_fej) ||
        !matrix_bit_equal(lhs.intrinsic_nominal, rhs.intrinsic_nominal) ||
        !matrix_bit_equal(lhs.intrinsic_fej, rhs.intrinsic_fej)) {
      return false;
    }
  }
  for (std::size_t index = 0; index < phase0.camera_caches.size(); ++index) {
    const CP2CameraModelCache &lhs = phase0.camera_caches[index];
    const CP2CameraModelCache &rhs = phase2.camera_caches[index];
    if (lhs.width != rhs.width || lhs.height != rhs.height ||
        !matrix_bit_equal(lhs.calibration, rhs.calibration) ||
        !matrix_bit_equal(lhs.K, rhs.K) || !matrix_bit_equal(lhs.D, rhs.D)) {
      return false;
    }
  }
  return true;
}

bool structurally_usable_status(CP2CompositeStateStatus status) noexcept {
  return status == CP2CompositeStateStatus::kAccepted ||
         status == CP2CompositeStateStatus::kNonfinite ||
         status == CP2CompositeStateStatus::kInvalidQuaternion;
}

bool phases_are_exact(const CP2CompositeStateSnapshot &phase0,
                      const CP2CompositeStateSnapshot &phase2,
                      const CP2CompositeStateSnapshot &phase3) noexcept {
  return phase0.phase == CP2StatePhase::kPhase0Prior &&
         phase2.phase == CP2StatePhase::kPhase2ExpectedPostcommit &&
         phase3.phase == CP2StatePhase::kPhase3LivePostcommit;
}

} // namespace

const char *cp2_commit_oracle_status_name(
    CP2CommitOracleStatus status) noexcept {
  switch (status) {
  case CP2CommitOracleStatus::kArithmeticOverflow:
    return "arithmetic_overflow";
  case CP2CommitOracleStatus::kInvalidPhase:
    return "invalid_phase";
  case CP2CommitOracleStatus::kStructuralMismatch:
    return "structural_mismatch";
  case CP2CommitOracleStatus::kComplete:
    return "complete";
  }
  return "unknown";
}

bool cp2_checked_eigen_index_to_u64(Eigen::Index value,
                                    std::uint64_t &output) noexcept {
  if (value < 0) {
    return false;
  }
  using UnsignedIndex = typename std::make_unsigned<Eigen::Index>::type;
  const UnsignedIndex unsigned_value = static_cast<UnsignedIndex>(value);
  if (std::numeric_limits<UnsignedIndex>::digits >
          std::numeric_limits<std::uint64_t>::digits &&
      unsigned_value > static_cast<UnsignedIndex>(
                           std::numeric_limits<std::uint64_t>::max())) {
    return false;
  }
  output = static_cast<std::uint64_t>(unsigned_value);
  return true;
}

bool cp2_checked_add_u64(std::uint64_t left, std::uint64_t right,
                         std::uint64_t &output) noexcept {
  if (right > std::numeric_limits<std::uint64_t>::max() - left) {
    return false;
  }
  output = left + right;
  return true;
}

bool cp2_checked_multiply_u64(std::uint64_t left, std::uint64_t right,
                              std::uint64_t &output) noexcept {
  if (left != 0U &&
      right > std::numeric_limits<std::uint64_t>::max() / left) {
    return false;
  }
  output = left * right;
  return true;
}

CP2CommitOracleResult CP2CommitOracle::Compare(
    const CP2CompositeStateSnapshot &phase0,
    const CP2CompositeStateSnapshot &phase2,
    const CP2CompositeStateSnapshot &phase3,
    std::uint64_t observed_type_update_calls) noexcept {
  CP2CommitOracleResult result;

  std::uint64_t expected_type_calls = 0;
  std::uint64_t phase0_blocks = 0;
  std::uint64_t phase3_blocks = 0;
  std::uint64_t expected_covariance_blocks = 0;
  std::uint64_t seen_covariance_blocks = 0;
  if (!size_to_u64(phase0.active_types.size(), expected_type_calls) ||
      !size_to_u64(phase0.semantic_blocks.size(), phase0_blocks) ||
      !size_to_u64(phase3.semantic_blocks.size(), phase3_blocks) ||
      !cp2_checked_multiply_u64(phase0_blocks, phase0_blocks,
                                expected_covariance_blocks) ||
      !cp2_checked_multiply_u64(phase3_blocks, phase3_blocks,
                                seen_covariance_blocks)) {
    return CP2CommitOracleResult();
  }

  result.observed_type_update_calls = observed_type_update_calls;
  result.baseline_expected_type_update_calls = expected_type_calls;
  result.type_update_calls_match =
      observed_type_update_calls == expected_type_calls;
  result.state_blocks_expected = phase0_blocks;
  result.state_blocks_seen = phase3_blocks;
  result.covariance_blocks_expected = expected_covariance_blocks;
  result.covariance_blocks_seen = seen_covariance_blocks;

  const CP2CompositeStateStatus phase0_status =
      CP2CompositeStateAdapter::Validate(phase0);
  const CP2CompositeStateStatus phase2_status =
      CP2CompositeStateAdapter::Validate(phase2);
  const CP2CompositeStateStatus phase3_status =
      CP2CompositeStateAdapter::Validate(phase3);
  result.phase0_valid = phase0_status == CP2CompositeStateStatus::kAccepted;
  result.phase2_valid = phase2_status == CP2CompositeStateStatus::kAccepted;
  result.phase3_valid = phase3_status == CP2CompositeStateStatus::kAccepted;
  result.complete_finite = snapshot_finite(phase0) && snapshot_finite(phase2) &&
                           snapshot_finite(phase3);

  if (!phases_are_exact(phase0, phase2, phase3)) {
    result.status = CP2CommitOracleStatus::kInvalidPhase;
    return result;
  }

  result.structure_equal = structurally_usable_status(phase0_status) &&
                           structurally_usable_status(phase2_status) &&
                           structurally_usable_status(phase3_status) &&
                           snapshot_structure_equal(phase0, phase2) &&
                           snapshot_structure_equal(phase0, phase3);
  if (!result.structure_equal) {
    result.status = CP2CommitOracleStatus::kStructuralMismatch;
    return result;
  }

  result.phase0_phase2_immutable_equal =
      immutable_prior_fields_equal(phase0, phase2);
  result.phase2_phase3_canonical_equal =
      snapshot_payload_equal(phase2, phase3);
  result.complete_canonical_equal =
      result.phase0_phase2_immutable_equal &&
      result.phase2_phase3_canonical_equal;

  std::uint64_t verified_nominal = 0;
  std::uint64_t nominal_mismatches = 0;
  std::uint64_t fej_mismatches = 0;
  for (std::size_t index = 0; index < phase0.active_types.size(); ++index) {
    std::uint64_t population = 0;
    std::uint64_t mismatches = 0;
    if (!matrix_mismatch_count(phase2.active_types[index].nominal,
                               phase3.active_types[index].nominal, population,
                               mismatches) ||
        !cp2_checked_add_u64(verified_nominal, population,
                             verified_nominal) ||
        !cp2_checked_add_u64(nominal_mismatches, mismatches,
                             nominal_mismatches)) {
      return CP2CommitOracleResult();
    }

    if (!matrix_mismatch_count(phase0.active_types[index].fej,
                               phase3.active_types[index].fej, population,
                               mismatches) ||
        !cp2_checked_add_u64(fej_mismatches, mismatches, fej_mismatches)) {
      return CP2CommitOracleResult();
    }
  }

  std::uint64_t covariance_population = 0;
  std::uint64_t covariance_mismatches = 0;
  if (!matrix_mismatch_count(phase2.covariance, phase3.covariance,
                             covariance_population,
                             covariance_mismatches)) {
    return CP2CommitOracleResult();
  }

  result.status = CP2CommitOracleStatus::kComplete;
  result.comparison_available = true;
  result.baseline_verified_nominal_fields = verified_nominal;
  result.baseline_nominal_mismatches = nominal_mismatches;
  result.baseline_covariance_mismatches = covariance_mismatches;
  result.baseline_fej_mismatches = fej_mismatches;
  result.passed = result.phase0_valid && result.phase2_valid &&
                  result.phase3_valid && result.complete_finite &&
                  result.complete_canonical_equal &&
                  result.type_update_calls_match && nominal_mismatches == 0U &&
                  covariance_mismatches == 0U && fej_mismatches == 0U;
  return result;
}

} // namespace ov_msckf
