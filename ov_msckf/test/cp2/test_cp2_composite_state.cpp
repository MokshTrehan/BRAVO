/*
 * SchurVIO-Lite CP2 composite-state, state-file, and detached-oracle tests.
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include "cam/CamRadtan.h"
#include "state/State.h"
#include "state/StateHelper.h"
#include "types/IMU.h"
#include "types/Landmark.h"
#include "types/LandmarkRepresentation.h"
#include "types/PoseJPL.h"
#include "types/Vec.h"
#include "update/CP2Canonical.h"
#include "update/CP2CompositeState.h"
#include "update/CP2StateTraceCodec.h"
#include "update/UpdaterMSCKFPreview.h"

#include <gtest/gtest.h>

#include <Eigen/Core>
#include <Eigen/Dense>

#include <algorithm>
#include <atomic>
#include <cerrno>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <functional>
#include <limits>
#include <memory>
#include <new>
#include <stdexcept>
#include <string>
#include <type_traits>
#include <utility>
#include <vector>

namespace cp2_allocation_probe {

std::atomic<bool> fail_new{false};
std::atomic<std::uint64_t> new_calls{0};

bool reject_allocation() noexcept {
  new_calls.fetch_add(1U, std::memory_order_relaxed);
  return fail_new.load(std::memory_order_relaxed);
}

} // namespace cp2_allocation_probe

// The composite-state test is a dedicated glibc executable. Interpose the C
// heap as well as every ordinary C++ new form so an Eigen malloc, an aligned
// allocation, or a library-level allocation cannot evade the postcommit
// failure-injection boundary.
#if defined(__GLIBC__)
extern "C" void *__libc_malloc(std::size_t);
extern "C" void *__libc_calloc(std::size_t, std::size_t);
extern "C" void *__libc_realloc(void *, std::size_t);
extern "C" void *__libc_memalign(std::size_t, std::size_t);
extern "C" void __libc_free(void *);

extern "C" void *malloc(std::size_t size) noexcept {
  if (cp2_allocation_probe::reject_allocation()) {
    return nullptr;
  }
  return __libc_malloc(size == 0U ? 1U : size);
}

extern "C" void *calloc(std::size_t count, std::size_t size) noexcept {
  if (cp2_allocation_probe::reject_allocation()) {
    return nullptr;
  }
  return __libc_calloc(count, size);
}

extern "C" void *realloc(void *memory, std::size_t size) noexcept {
  if (cp2_allocation_probe::reject_allocation()) {
    return nullptr;
  }
  return __libc_realloc(memory, size);
}

extern "C" void *aligned_alloc(std::size_t alignment,
                               std::size_t size) noexcept {
  if (cp2_allocation_probe::reject_allocation()) {
    return nullptr;
  }
  return __libc_memalign(alignment, size);
}

extern "C" int posix_memalign(void **memory, std::size_t alignment,
                              std::size_t size) noexcept {
  if (cp2_allocation_probe::reject_allocation()) {
    *memory = nullptr;
    return ENOMEM;
  }
  void *allocated = __libc_memalign(alignment, size);
  if (allocated == nullptr) {
    *memory = nullptr;
    return ENOMEM;
  }
  *memory = allocated;
  return 0;
}

extern "C" void free(void *memory) noexcept { __libc_free(memory); }
#endif

void *operator new(std::size_t size) {
  if (cp2_allocation_probe::reject_allocation()) {
    throw std::bad_alloc();
  }
#if defined(__GLIBC__)
  if (void *memory = __libc_malloc(size == 0U ? 1U : size)) {
#else
  if (void *memory = std::malloc(size == 0U ? 1U : size)) {
#endif
    return memory;
  }
  throw std::bad_alloc();
}

void *operator new[](std::size_t size) {
  if (cp2_allocation_probe::reject_allocation()) {
    throw std::bad_alloc();
  }
#if defined(__GLIBC__)
  if (void *memory = __libc_malloc(size == 0U ? 1U : size)) {
#else
  if (void *memory = std::malloc(size == 0U ? 1U : size)) {
#endif
    return memory;
  }
  throw std::bad_alloc();
}

void *operator new(std::size_t size, const std::nothrow_t &) noexcept {
  if (cp2_allocation_probe::reject_allocation()) {
    return nullptr;
  }
#if defined(__GLIBC__)
  return __libc_malloc(size == 0U ? 1U : size);
#else
  return std::malloc(size == 0U ? 1U : size);
#endif
}

void *operator new[](std::size_t size, const std::nothrow_t &) noexcept {
  return ::operator new(size, std::nothrow);
}

#if defined(__cpp_aligned_new)
void *operator new(std::size_t size, std::align_val_t alignment) {
  if (cp2_allocation_probe::reject_allocation()) {
    throw std::bad_alloc();
  }
#if defined(__GLIBC__)
  void *memory = __libc_memalign(static_cast<std::size_t>(alignment), size);
#else
  void *memory = std::aligned_alloc(static_cast<std::size_t>(alignment), size);
#endif
  if (memory != nullptr) {
    return memory;
  }
  throw std::bad_alloc();
}

void *operator new[](std::size_t size, std::align_val_t alignment) {
  return ::operator new(size, alignment);
}

void *operator new(std::size_t size, std::align_val_t alignment,
                   const std::nothrow_t &) noexcept {
  try {
    return ::operator new(size, alignment);
  } catch (...) {
    return nullptr;
  }
}

void *operator new[](std::size_t size, std::align_val_t alignment,
                     const std::nothrow_t &) noexcept {
  return ::operator new(size, alignment, std::nothrow);
}
#endif

void operator delete(void *memory) noexcept { free(memory); }
void operator delete[](void *memory) noexcept { free(memory); }
void operator delete(void *memory, std::size_t) noexcept { free(memory); }
void operator delete[](void *memory, std::size_t) noexcept { free(memory); }
void operator delete(void *memory, const std::nothrow_t &) noexcept { free(memory); }
void operator delete[](void *memory, const std::nothrow_t &) noexcept {
  free(memory);
}

#if defined(__cpp_aligned_new)
void operator delete(void *memory, std::align_val_t) noexcept { free(memory); }
void operator delete[](void *memory, std::align_val_t) noexcept { free(memory); }
void operator delete(void *memory, std::size_t, std::align_val_t) noexcept {
  free(memory);
}
void operator delete[](void *memory, std::size_t,
                       std::align_val_t) noexcept {
  free(memory);
}
void operator delete(void *memory, std::align_val_t,
                     const std::nothrow_t &) noexcept {
  free(memory);
}
void operator delete[](void *memory, std::align_val_t,
                       const std::nothrow_t &) noexcept {
  free(memory);
}
#endif

namespace {

using ov_msckf::CP2ActiveStateType;
using ov_msckf::CP2ActiveStateTypeTag;
using ov_msckf::CP2CameraFixedCalibration;
using ov_msckf::CP2CameraModelCache;
using ov_msckf::CP2CompositeStateAdapter;
using ov_msckf::CP2CompositeStateCapture;
using ov_msckf::CP2CompositeStateSnapshot;
using ov_msckf::CP2CompositeStateStatus;
using ov_msckf::CP2FixedImuCalibration;
using ov_msckf::CP2FixedImuCalibrationTag;
using ov_msckf::CP2InvocationTraceIdentity;
using ov_msckf::CP2LandmarkIdentity;
using ov_msckf::CP2LandmarkRepresentation;
using ov_msckf::CP2PreparedPostcommitCapture;
using ov_msckf::CP2SemanticStateBlock;
using ov_msckf::CP2SemanticStateKind;
using ov_msckf::CP2StatePhase;
using ov_msckf::CP2StateTraceCodec;
using ov_msckf::CP2StateTraceFrame;

std::uint64_t binary64_bits(double value) noexcept {
  static_assert(sizeof(double) == sizeof(std::uint64_t),
                "CP2 requires IEEE-754 binary64 doubles");
  std::uint64_t bits = 0U;
  std::memcpy(&bits, &value, sizeof(bits));
  return bits;
}

double binary64_from_bits(std::uint64_t bits) noexcept {
  double value = 0.0;
  std::memcpy(&value, &bits, sizeof(value));
  return value;
}

template <typename LeftDerived, typename RightDerived>
bool matrix_bits_equal(const Eigen::MatrixBase<LeftDerived> &left,
                       const Eigen::MatrixBase<RightDerived> &right) noexcept {
  if (left.rows() != right.rows() || left.cols() != right.cols()) {
    return false;
  }
  for (Eigen::Index row = 0; row < left.rows(); ++row) {
    for (Eigen::Index column = 0; column < left.cols(); ++column) {
      if (binary64_bits(left(row, column)) !=
          binary64_bits(right(row, column))) {
        return false;
      }
    }
  }
  return true;
}

Eigen::Vector4d unit_quaternion(unsigned int role) {
  switch (role % 6U) {
  case 0U:
    return Eigen::Vector4d(0.0, 0.0, 0.0, 1.0);
  case 1U:
    return Eigen::Vector4d(1.0, 0.0, 0.0, 0.0);
  case 2U:
    return Eigen::Vector4d(0.0, 1.0, 0.0, 0.0);
  case 3U:
    return Eigen::Vector4d(0.0, 0.0, 1.0, 0.0);
  case 4U:
    return Eigen::Vector4d(0.5, 0.5, 0.5, 0.5);
  default:
    return Eigen::Vector4d(-0.5, 0.5, -0.5, 0.5);
  }
}

Eigen::MatrixXd column(std::initializer_list<double> values) {
  Eigen::MatrixXd result(static_cast<Eigen::Index>(values.size()), 1);
  Eigen::Index index = 0;
  for (double value : values) {
    result(index++, 0) = value;
  }
  return result;
}

Eigen::MatrixXd imu_value(unsigned int role, double base) {
  Eigen::MatrixXd result(16, 1);
  result.topRows(4) = unit_quaternion(role);
  for (Eigen::Index index = 4; index < result.rows(); ++index) {
    result(index, 0) = base + 0.125 * static_cast<double>(index - 3);
  }
  return result;
}

Eigen::MatrixXd pose_value(unsigned int role, double base) {
  Eigen::MatrixXd result(7, 1);
  result.topRows(4) = unit_quaternion(role);
  result(4, 0) = base + 0.25;
  result(5, 0) = base - 0.50;
  result(6, 0) = base + 0.75;
  return result;
}

CP2LandmarkIdentity landmark_identity(
    std::uint64_t feature_id, CP2LandmarkRepresentation representation,
    std::int64_t camera_id, double anchor_timestamp) {
  CP2LandmarkIdentity identity;
  identity.feature_id = feature_id;
  identity.representation = representation;
  identity.anchor_camera_id = camera_id;
  identity.anchor_timestamp = anchor_timestamp;
  return identity;
}

CP2SemanticStateBlock semantic_block(CP2SemanticStateKind kind,
                                      std::uint64_t covariance_id,
                                      std::uint64_t error_size) {
  CP2SemanticStateBlock block;
  block.kind = kind;
  block.covariance_id = covariance_id;
  block.error_size = error_size;
  return block;
}

CP2ActiveStateType active_type(CP2ActiveStateTypeTag tag,
                               std::uint64_t covariance_id,
                               std::uint64_t error_size,
                               const Eigen::MatrixXd &nominal,
                               const Eigen::MatrixXd &fej) {
  CP2ActiveStateType type;
  type.tag = tag;
  type.covariance_id = covariance_id;
  type.error_size = error_size;
  type.nominal = nominal;
  type.fej = fej;
  return type;
}

CP2FixedImuCalibration fixed_imu(CP2FixedImuCalibrationTag tag,
                                 const Eigen::MatrixXd &nominal,
                                 const Eigen::MatrixXd &fej) {
  CP2FixedImuCalibration calibration;
  calibration.tag = tag;
  calibration.nominal = nominal;
  calibration.fej = fej;
  return calibration;
}

CP2CompositeStateSnapshot full_snapshot(CP2StatePhase phase) {
  CP2CompositeStateSnapshot snapshot;
  snapshot.phase = phase;
  snapshot.timestamp = 42.25;

  // Active error-state order: IMU(15), two clones(6 each), then every
  // landmark representation (3,3,3,3,3,1), for n=43.
  constexpr std::uint64_t dimension = 43U;
  snapshot.covariance = Eigen::MatrixXd::Zero(dimension, dimension);
  for (Eigen::Index row = 0; row < snapshot.covariance.rows(); ++row) {
    for (Eigen::Index col = 0; col <= row; ++col) {
      const double value = row == col
                               ? 2.0 + 0.01 * static_cast<double>(row)
                               : 0.0001 * static_cast<double>((row + 1) * (col + 2));
      snapshot.covariance(row, col) = value;
      snapshot.covariance(col, row) = value;
    }
  }

  snapshot.semantic_blocks = {
      semantic_block(CP2SemanticStateKind::kImuTheta, 0U, 3U),
      semantic_block(CP2SemanticStateKind::kImuPosition, 3U, 3U),
      semantic_block(CP2SemanticStateKind::kImuVelocity, 6U, 3U),
      semantic_block(CP2SemanticStateKind::kImuGyroBias, 9U, 3U),
      semantic_block(CP2SemanticStateKind::kImuAccelBias, 12U, 3U),
      semantic_block(CP2SemanticStateKind::kCloneTheta, 15U, 3U),
      semantic_block(CP2SemanticStateKind::kClonePosition, 18U, 3U),
      semantic_block(CP2SemanticStateKind::kCloneTheta, 21U, 3U),
      semantic_block(CP2SemanticStateKind::kClonePosition, 24U, 3U),
      semantic_block(CP2SemanticStateKind::kSlamLandmark, 27U, 3U),
      semantic_block(CP2SemanticStateKind::kSlamLandmark, 30U, 3U),
      semantic_block(CP2SemanticStateKind::kSlamLandmark, 33U, 3U),
      semantic_block(CP2SemanticStateKind::kSlamLandmark, 36U, 3U),
      semantic_block(CP2SemanticStateKind::kSlamLandmark, 39U, 3U),
      semantic_block(CP2SemanticStateKind::kSlamLandmark, 42U, 1U),
  };
  snapshot.semantic_blocks[5].clone_timestamp = 1.25;
  snapshot.semantic_blocks[6].clone_timestamp = 1.25;
  snapshot.semantic_blocks[7].clone_timestamp = 2.5;
  snapshot.semantic_blocks[8].clone_timestamp = 2.5;

  const std::vector<CP2LandmarkIdentity> identities = {
      landmark_identity(101U, CP2LandmarkRepresentation::kGlobal3D, -1, -1.0),
      landmark_identity(102U, CP2LandmarkRepresentation::kGlobalFullInverseDepth,
                        -1, -1.0),
      landmark_identity(103U, CP2LandmarkRepresentation::kAnchored3D, 0, 1.25),
      landmark_identity(104U, CP2LandmarkRepresentation::kAnchoredFullInverseDepth,
                        1, 2.5),
      landmark_identity(105U, CP2LandmarkRepresentation::kAnchoredMsckfInverseDepth,
                        0, 2.5),
      landmark_identity(106U, CP2LandmarkRepresentation::kAnchoredInverseDepthSingle,
                        1, 1.25),
  };
  for (std::size_t index = 0; index < identities.size(); ++index) {
    snapshot.semantic_blocks[9U + index].landmark = identities[index];
  }

  CP2ActiveStateType imu = active_type(CP2ActiveStateTypeTag::kImu, 0U, 15U,
                                       imu_value(4U, 0.25), imu_value(5U, -0.25));
  snapshot.active_types.push_back(imu);

  CP2ActiveStateType first_clone = active_type(
      CP2ActiveStateTypeTag::kClone, 15U, 6U, pose_value(0U, 1.0),
      pose_value(1U, 1.5));
  first_clone.clone_timestamp = 1.25;
  snapshot.active_types.push_back(first_clone);
  CP2ActiveStateType second_clone = active_type(
      CP2ActiveStateTypeTag::kClone, 21U, 6U, pose_value(2U, 2.0),
      pose_value(3U, 2.5));
  second_clone.clone_timestamp = 2.5;
  snapshot.active_types.push_back(second_clone);

  std::uint64_t covariance_id = 27U;
  for (std::size_t index = 0; index < identities.size(); ++index) {
    const std::uint64_t size = index + 1U == identities.size() ? 1U : 3U;
    Eigen::MatrixXd nominal(size, 1);
    Eigen::MatrixXd fej(size, 1);
    for (Eigen::Index coefficient = 0; coefficient < nominal.rows(); ++coefficient) {
      nominal(coefficient, 0) = 3.0 + static_cast<double>(index) +
                                0.1 * static_cast<double>(coefficient);
      fej(coefficient, 0) = -3.0 - static_cast<double>(index) -
                            0.2 * static_cast<double>(coefficient);
    }
    CP2ActiveStateType landmark = active_type(
        CP2ActiveStateTypeTag::kSlamLandmark, covariance_id, size, nominal, fej);
    landmark.landmark = identities[index];
    snapshot.active_types.push_back(landmark);
    covariance_id += size;
  }

  snapshot.fixed_imu_calibrations = {
      fixed_imu(CP2FixedImuCalibrationTag::kDw,
                column({1.0, 0.01, 0.02, 1.1, 0.03, 0.9}),
                column({1.0, -0.01, -0.02, 1.1, -0.03, 0.9})),
      fixed_imu(CP2FixedImuCalibrationTag::kDa,
                column({0.98, 0.04, 0.05, 1.02, 0.06, 1.01}),
                column({0.97, -0.04, -0.05, 1.03, -0.06, 1.00})),
      fixed_imu(CP2FixedImuCalibrationTag::kTg,
                column({0.001, 0.002, 0.003, 0.004, 0.005, 0.006, 0.007,
                        0.008, 0.009}),
                column({-0.001, -0.002, -0.003, -0.004, -0.005, -0.006,
                        -0.007, -0.008, -0.009})),
      fixed_imu(CP2FixedImuCalibrationTag::kGyroToImu, unit_quaternion(0U),
                unit_quaternion(1U)),
      fixed_imu(CP2FixedImuCalibrationTag::kAccelToImu, unit_quaternion(2U),
                unit_quaternion(3U)),
  };
  snapshot.time_offset_nominal = column({0.0025});
  snapshot.time_offset_fej = column({-0.00125});

  for (std::uint64_t camera_id = 0; camera_id < 2U; ++camera_id) {
    CP2CameraFixedCalibration camera;
    camera.camera_id = camera_id;
    camera.extrinsic_nominal =
        pose_value(static_cast<unsigned int>(camera_id + 2U),
                   0.10 * static_cast<double>(camera_id + 1U));
    camera.extrinsic_fej =
        pose_value(static_cast<unsigned int>(camera_id + 4U),
                   -0.10 * static_cast<double>(camera_id + 1U));
    camera.intrinsic_nominal =
        column({450.0 + static_cast<double>(camera_id),
                451.0 + static_cast<double>(camera_id), 376.0, 240.0,
                -0.28, 0.07, 0.0002, -0.0001});
    camera.intrinsic_fej = camera.intrinsic_nominal;
    camera.intrinsic_fej(0, 0) += 0.125;
    snapshot.camera_calibrations.push_back(camera);

    CP2CameraModelCache cache;
    cache.camera_id = camera_id;
    cache.width = 752;
    cache.height = 480;
    cache.calibration = camera.intrinsic_nominal;
    cache.K = Eigen::Matrix3d::Identity();
    cache.K(0, 0) = cache.calibration(0, 0);
    cache.K(1, 1) = cache.calibration(1, 0);
    cache.K(0, 2) = cache.calibration(2, 0);
    cache.K(1, 2) = cache.calibration(3, 0);
    cache.D = cache.calibration.bottomRows(4);
    snapshot.camera_caches.push_back(cache);
  }
  return snapshot;
}

struct LiveCompositeFixture {
  std::shared_ptr<ov_msckf::State> state;
  std::vector<std::shared_ptr<ov_type::Type>> top_level;
};

// Test-only access to State's active-vector association. The production class
// deliberately keeps this member private; the adversarial pointer-graph test
// must nevertheless perturb the retained order and membership themselves,
// instead of approximating those faults through a public side structure.
template <typename Tag, typename Tag::type Member> struct PrivateMemberAccess {
  friend typename Tag::type cp2_private_member(Tag) { return Member; }
};

struct StateVariablesMember {
  using type = std::vector<std::shared_ptr<ov_type::Type>> ov_msckf::State::*;
  friend type cp2_private_member(StateVariablesMember);
};

template struct PrivateMemberAccess<StateVariablesMember,
                                    &ov_msckf::State::_variables>;

std::vector<std::shared_ptr<ov_type::Type>> &
active_variables(ov_msckf::State &state) {
  return state.*cp2_private_member(StateVariablesMember{});
}

class MutablePoseJPL final : public ov_type::PoseJPL {
public:
  void force_parent_id_for_overflow_test(int value) noexcept { _id = value; }

  void replace_quaternion(std::shared_ptr<ov_type::JPLQuat> replacement) {
    _q = std::move(replacement);
  }

  void replace_position(std::shared_ptr<ov_type::Vec> replacement) {
    _p = std::move(replacement);
  }
};

class MutableIMU final : public ov_type::IMU {
public:
  void force_parent_id_for_overflow_test(int value) noexcept { _id = value; }

  void replace_pose(std::shared_ptr<ov_type::PoseJPL> replacement) {
    _pose = std::move(replacement);
  }

  void replace_velocity(std::shared_ptr<ov_type::Vec> replacement) {
    _v = std::move(replacement);
  }

  void replace_gyro_bias(std::shared_ptr<ov_type::Vec> replacement) {
    _bg = std::move(replacement);
  }

  void replace_accel_bias(std::shared_ptr<ov_type::Vec> replacement) {
    _ba = std::move(replacement);
  }
};

void initialize_live_landmark(
    const std::shared_ptr<ov_msckf::State> &state,
    const std::shared_ptr<ov_type::Landmark> &landmark) {
  const Eigen::Index dimension = landmark->size();
  // Use an explicit zero Jacobian against a real active type. Eigen 3.3's
  // triangular-product kernel dereferences a null coefficient for a dynamic
  // matrix product with a zero-width inner dimension under UBSan, even though
  // that product is mathematically zero. This production initializer call is
  // equivalent, keeps every cross-covariance exactly zero, and avoids relying
  // on that undefined library edge.
  const std::vector<std::shared_ptr<ov_type::Type>> zero_order{state->_imu};
  const Eigen::MatrixXd H_R =
      Eigen::MatrixXd::Zero(dimension, state->_imu->size());
  const Eigen::MatrixXd H_L = Eigen::MatrixXd::Identity(dimension, dimension);
  const Eigen::MatrixXd R = 0.25 * Eigen::MatrixXd::Identity(dimension, dimension);
  const Eigen::VectorXd residual = Eigen::VectorXd::Zero(dimension);
  ov_msckf::StateHelper::initialize_invertible(state, landmark, zero_order, H_R,
                                               H_L, R, residual);
  state->_features_SLAM.emplace(landmark->_featid, landmark);
}

LiveCompositeFixture live_fixture(bool shuffle_unordered_maps = false) {
  ov_msckf::StateOptions options;
  options.do_fej = true;
  options.do_calib_camera_pose = false;
  options.do_calib_camera_intrinsics = false;
  options.do_calib_camera_timeoffset = false;
  options.do_calib_imu_intrinsics = false;
  options.do_calib_imu_g_sensitivity = false;
  options.num_cameras = 2;
  options.max_clone_size = 4;

  LiveCompositeFixture fixture;
  fixture.state = std::make_shared<ov_msckf::State>(options);
  fixture.state->_imu->set_value(imu_value(4U, 0.25));
  fixture.state->_imu->set_fej(imu_value(5U, -0.25));

  fixture.state->_calib_imu_dw->set_value(
      column({1.0, 0.01, 0.02, 1.1, 0.03, 0.9}));
  fixture.state->_calib_imu_dw->set_fej(
      column({1.0, -0.01, -0.02, 1.1, -0.03, 0.9}));
  fixture.state->_calib_imu_da->set_value(
      column({0.98, 0.04, 0.05, 1.02, 0.06, 1.01}));
  fixture.state->_calib_imu_da->set_fej(
      column({0.97, -0.04, -0.05, 1.03, -0.06, 1.00}));
  fixture.state->_calib_imu_tg->set_value(
      column({0.001, 0.002, 0.003, 0.004, 0.005, 0.006, 0.007, 0.008,
              0.009}));
  fixture.state->_calib_imu_tg->set_fej(
      column({-0.001, -0.002, -0.003, -0.004, -0.005, -0.006, -0.007,
              -0.008, -0.009}));
  fixture.state->_calib_imu_GYROtoIMU->set_value(unit_quaternion(0U));
  fixture.state->_calib_imu_GYROtoIMU->set_fej(unit_quaternion(1U));
  fixture.state->_calib_imu_ACCtoIMU->set_value(unit_quaternion(2U));
  fixture.state->_calib_imu_ACCtoIMU->set_fej(unit_quaternion(3U));
  fixture.state->_calib_dt_CAMtoIMU->set_value(column({0.0025}));
  fixture.state->_calib_dt_CAMtoIMU->set_fej(column({-0.00125}));

  for (std::size_t camera_id = 0; camera_id < 2U; ++camera_id) {
    const Eigen::MatrixXd extrinsic =
        pose_value(static_cast<unsigned int>(camera_id + 2U),
                   0.10 * static_cast<double>(camera_id + 1U));
    const Eigen::MatrixXd extrinsic_fej =
        pose_value(static_cast<unsigned int>(camera_id + 4U),
                   -0.10 * static_cast<double>(camera_id + 1U));
    Eigen::MatrixXd intrinsics =
        column({450.0 + static_cast<double>(camera_id),
                451.0 + static_cast<double>(camera_id), 376.0, 240.0,
                -0.28, 0.07, 0.0002, -0.0001});
    Eigen::MatrixXd intrinsics_fej = intrinsics;
    intrinsics_fej(0, 0) += 0.125;
    fixture.state->_calib_IMUtoCAM.at(camera_id)->set_value(extrinsic);
    fixture.state->_calib_IMUtoCAM.at(camera_id)->set_fej(extrinsic_fej);
    fixture.state->_cam_intrinsics.at(camera_id)->set_value(intrinsics);
    fixture.state->_cam_intrinsics.at(camera_id)->set_fej(intrinsics_fej);
    auto cache = std::make_shared<ov_core::CamRadtan>(752, 480);
    cache->set_value(intrinsics);
    fixture.state->_cam_intrinsics_cameras[camera_id] = cache;
  }

  const std::vector<double> clone_timestamps{1.25, 2.5};
  for (std::size_t index = 0; index < clone_timestamps.size(); ++index) {
    fixture.state->_imu->set_value(
        imu_value(static_cast<unsigned int>(index), 1.0 + index));
    fixture.state->_imu->set_fej(
        imu_value(static_cast<unsigned int>(index + 2U), 1.5 + index));
    fixture.state->_timestamp = clone_timestamps[index];
    ov_msckf::StateHelper::augment_clone(fixture.state,
                                         Eigen::Vector3d::Zero());
  }

  auto global = std::make_shared<ov_type::Landmark>(3);
  global->_featid = 501U;
  global->_feat_representation =
      ov_type::LandmarkRepresentation::Representation::GLOBAL_3D;
  global->_anchor_cam_id = -1;
  global->_anchor_clone_timestamp = -1.0;
  global->set_value(column({3.0, 0.25, 5.0}));
  global->set_fej(column({3.1, 0.20, 4.9}));
  initialize_live_landmark(fixture.state, global);

  auto anchored = std::make_shared<ov_type::Landmark>(1);
  anchored->_featid = 502U;
  anchored->_feat_representation =
      ov_type::LandmarkRepresentation::Representation::ANCHORED_INVERSE_DEPTH_SINGLE;
  anchored->_anchor_cam_id = 1;
  anchored->_anchor_clone_timestamp = 2.5;
  anchored->set_value(column({0.20}));
  anchored->set_fej(column({0.21}));
  initialize_live_landmark(fixture.state, anchored);

  fixture.top_level.push_back(fixture.state->_imu);
  fixture.top_level.push_back(fixture.state->_clones_IMU.at(1.25));
  fixture.top_level.push_back(fixture.state->_clones_IMU.at(2.5));
  fixture.top_level.push_back(global);
  fixture.top_level.push_back(anchored);
  if (fixture.state->max_covariance_size() != 31) {
    throw std::logic_error("live composite fixture has an unexpected state dimension");
  }

  Eigen::MatrixXd root = Eigen::MatrixXd::Zero(31, 31);
  for (Eigen::Index row = 0; row < root.rows(); ++row) {
    root(row, row) = 0.20 + 0.002 * static_cast<double>(row + 1);
    for (Eigen::Index col = 0; col < row; ++col) {
      root(row, col) =
          0.001 * std::sin(0.17 * static_cast<double>((row + 1) * (col + 2)));
    }
  }
  ov_msckf::StateHelper::set_initial_covariance(fixture.state,
                                                root * root.transpose(),
                                                fixture.top_level);
  fixture.state->_timestamp = 3.75;

  if (shuffle_unordered_maps) {
    const std::shared_ptr<ov_type::Landmark> first =
        fixture.state->_features_SLAM.at(501U);
    const std::shared_ptr<ov_type::Landmark> second =
        fixture.state->_features_SLAM.at(502U);
    fixture.state->_features_SLAM.clear();
    fixture.state->_features_SLAM.rehash(53U);
    fixture.state->_features_SLAM.emplace(502U, second);
    fixture.state->_features_SLAM.emplace(501U, first);

    fixture.state->_calib_IMUtoCAM.rehash(53U);
    fixture.state->_cam_intrinsics.rehash(47U);
    fixture.state->_cam_intrinsics_cameras.rehash(43U);
  }
  return fixture;
}

std::shared_ptr<MutablePoseJPL> mutable_pose_copy(
    const std::shared_ptr<ov_type::PoseJPL> &source) {
  auto replacement = std::make_shared<MutablePoseJPL>();
  replacement->set_value(source->value());
  replacement->set_fej(source->fej());
  replacement->set_local_id(source->id());
  return replacement;
}

std::shared_ptr<MutableIMU> install_mutable_imu(
    LiveCompositeFixture &fixture, bool mutable_pose) {
  const std::shared_ptr<ov_type::IMU> source = fixture.state->_imu;
  auto replacement = std::make_shared<MutableIMU>();
  replacement->set_value(source->value());
  replacement->set_fej(source->fej());
  replacement->set_local_id(source->id());
  if (mutable_pose) {
    replacement->replace_pose(mutable_pose_copy(source->pose()));
    replacement->set_local_id(source->id());
  }
  fixture.state->_imu = replacement;
  active_variables(*fixture.state).at(0) = replacement;
  fixture.top_level.at(0) = replacement;
  return replacement;
}

std::shared_ptr<MutablePoseJPL> install_mutable_clone(
    LiveCompositeFixture &fixture, double timestamp) {
  const std::shared_ptr<ov_type::PoseJPL> source =
      fixture.state->_clones_IMU.at(timestamp);
  const std::shared_ptr<MutablePoseJPL> replacement = mutable_pose_copy(source);
  fixture.state->_clones_IMU.at(timestamp) = replacement;
  for (std::shared_ptr<ov_type::Type> &variable :
       active_variables(*fixture.state)) {
    if (variable.get() == source.get()) {
      variable = replacement;
    }
  }
  for (std::shared_ptr<ov_type::Type> &variable : fixture.top_level) {
    if (variable.get() == source.get()) {
      variable = replacement;
    }
  }
  return replacement;
}

template <typename Prepare, typename Mutate>
void expect_pointer_graph_mutation_detected(Prepare prepare, Mutate mutate) {
  LiveCompositeFixture fixture = live_fixture(false);
  prepare(fixture);
  CP2CompositeStateCapture captured;
  ASSERT_EQ(CP2CompositeStateAdapter::Capture(
                fixture.state, CP2StatePhase::kPhase0Prior, captured),
            CP2CompositeStateStatus::kAccepted);
  ASSERT_TRUE(CP2CompositeStateAdapter::PointerGraphMatches(
      fixture.state, captured.pointer_graph));
  mutate(fixture);
  EXPECT_FALSE(CP2CompositeStateAdapter::PointerGraphMatches(
      fixture.state, captured.pointer_graph));
}

template <typename Mutate>
void expect_pointer_graph_mutation_detected(Mutate mutate) {
  expect_pointer_graph_mutation_detected(
      [](LiveCompositeFixture &) {}, std::move(mutate));
}

std::string sha256_hex(const std::vector<std::uint8_t> &bytes) {
  ov_msckf::CP2Sha256 sha;
  sha.Update(bytes);
  return sha.HexDigest();
}

void store_u64(std::vector<std::uint8_t> &bytes, std::size_t offset,
               std::uint64_t value) {
  ASSERT_LE(offset + 8U, bytes.size());
  for (std::size_t index = 0; index < 8U; ++index) {
    bytes[offset + index] =
        static_cast<std::uint8_t>(value >> (56U - 8U * index));
  }
}

void expect_encode_detects_mutation(const CP2CompositeStateSnapshot &baseline,
                                    const CP2CompositeStateSnapshot &mutated) {
  const std::vector<std::uint8_t> baseline_bytes =
      CP2StateTraceCodec::EncodeSnapshotPayload(baseline);
  try {
    EXPECT_NE(CP2StateTraceCodec::EncodeSnapshotPayload(mutated), baseline_bytes);
  } catch (const ov_msckf::CP2TraceCodecError &) {
    SUCCEED();
  }
}

void set_quaternion(Eigen::MatrixXd &matrix, const Eigen::Vector4d &quaternion) {
  ASSERT_GE(matrix.rows(), 4);
  ASSERT_EQ(matrix.cols(), 1);
  matrix.topRows(4) = quaternion;
}

std::vector<Eigen::MatrixXd *> quaternion_fields(CP2CompositeStateSnapshot &snapshot) {
  std::vector<Eigen::MatrixXd *> fields;
  fields.push_back(&snapshot.active_types.at(0).nominal);
  fields.push_back(&snapshot.active_types.at(0).fej);
  fields.push_back(&snapshot.active_types.at(1).nominal);
  fields.push_back(&snapshot.active_types.at(1).fej);
  fields.push_back(&snapshot.active_types.at(2).nominal);
  fields.push_back(&snapshot.active_types.at(2).fej);
  fields.push_back(&snapshot.fixed_imu_calibrations.at(3).nominal);
  fields.push_back(&snapshot.fixed_imu_calibrations.at(3).fej);
  fields.push_back(&snapshot.fixed_imu_calibrations.at(4).nominal);
  fields.push_back(&snapshot.fixed_imu_calibrations.at(4).fej);
  for (CP2CameraFixedCalibration &camera : snapshot.camera_calibrations) {
    fields.push_back(&camera.extrinsic_nominal);
    fields.push_back(&camera.extrinsic_fej);
  }
  return fields;
}

std::vector<Eigen::MatrixXd *> all_matrix_fields(
    CP2CompositeStateSnapshot &snapshot) {
  std::vector<Eigen::MatrixXd *> fields;
  fields.push_back(&snapshot.covariance);
  for (CP2ActiveStateType &type : snapshot.active_types) {
    fields.push_back(&type.nominal);
    fields.push_back(&type.fej);
  }
  for (CP2FixedImuCalibration &calibration :
       snapshot.fixed_imu_calibrations) {
    fields.push_back(&calibration.nominal);
    fields.push_back(&calibration.fej);
  }
  fields.push_back(&snapshot.time_offset_nominal);
  fields.push_back(&snapshot.time_offset_fej);
  for (CP2CameraFixedCalibration &camera : snapshot.camera_calibrations) {
    fields.push_back(&camera.extrinsic_nominal);
    fields.push_back(&camera.extrinsic_fej);
    fields.push_back(&camera.intrinsic_nominal);
    fields.push_back(&camera.intrinsic_fej);
  }
  for (CP2CameraModelCache &cache : snapshot.camera_caches) {
    fields.push_back(&cache.calibration);
    fields.push_back(&cache.K);
    fields.push_back(&cache.D);
  }
  return fields;
}

TEST(CP2CompositeStateCodec, FrozenFullRolePayloadRoundTripsBitExactly) {
  const CP2CompositeStateSnapshot snapshot =
      full_snapshot(CP2StatePhase::kPhase0Prior);
  ASSERT_EQ(CP2CompositeStateAdapter::Validate(snapshot),
            CP2CompositeStateStatus::kAccepted);
  const std::vector<std::uint8_t> payload =
      CP2StateTraceCodec::EncodeSnapshotPayload(snapshot);

  const std::string domain("SchurVIO-CP2-prior-snapshot-v1\0", 31U);
  ASSERT_GE(payload.size(), domain.size());
  EXPECT_TRUE(std::equal(domain.begin(), domain.end(), payload.begin()));
  // The explicit reviewed size/digest pair prevents a self-consistent
  // encoder/decoder change from silently redefining the file contract.
  EXPECT_EQ(payload.size(), 19335U);
  EXPECT_EQ(sha256_hex(payload),
            "6d6ab4536c957dd9eed060f7cc32cd03e2d2d270df77f24098e35823bebeed44");

  const CP2CompositeStateSnapshot decoded =
      CP2StateTraceCodec::DecodeSnapshotPayload(
          payload, CP2StatePhase::kPhase0Prior);
  EXPECT_TRUE(CP2CompositeStateAdapter::CanonicallyEqual(snapshot, decoded));
  EXPECT_EQ(decoded.phase, CP2StatePhase::kPhase0Prior);
  EXPECT_EQ(CP2StateTraceCodec::EncodeSnapshotPayload(decoded), payload);

  CP2CompositeStateSnapshot post = snapshot;
  post.phase = CP2StatePhase::kPhase2ExpectedPostcommit;
  const std::vector<std::uint8_t> post_payload =
      CP2StateTraceCodec::EncodeSnapshotPayload(post);
  const std::string post_domain("SchurVIO-CP2-postcommit-state-v1\0", 33U);
  ASSERT_GE(post_payload.size(), post_domain.size());
  EXPECT_TRUE(std::equal(post_domain.begin(), post_domain.end(),
                         post_payload.begin()));
  EXPECT_FALSE(CP2CompositeStateAdapter::CanonicallyEqual(snapshot, post));
}

TEST(CP2CompositeStateCodec, EveryBinary64CoefficientMutationIsDetected) {
  CP2CompositeStateSnapshot snapshot =
      full_snapshot(CP2StatePhase::kPhase0Prior);
  const CP2CompositeStateSnapshot baseline = snapshot;
  std::uint64_t mutations = 0U;

  const auto mutate_scalar = [&](double &coefficient) {
    const double original = coefficient;
    coefficient = binary64_from_bits(binary64_bits(original) ^ UINT64_C(1));
    expect_encode_detects_mutation(baseline, snapshot);
    coefficient = original;
    ++mutations;
  };
  const auto mutate_matrix = [&](Eigen::MatrixXd &matrix) {
    for (Eigen::Index row = 0; row < matrix.rows(); ++row) {
      for (Eigen::Index column_index = 0; column_index < matrix.cols();
           ++column_index) {
        mutate_scalar(matrix(row, column_index));
      }
    }
  };

  mutate_scalar(snapshot.timestamp);
  mutate_matrix(snapshot.covariance);
  for (CP2SemanticStateBlock &block : snapshot.semantic_blocks) {
    if (block.kind == CP2SemanticStateKind::kCloneTheta ||
        block.kind == CP2SemanticStateKind::kClonePosition) {
      mutate_scalar(block.clone_timestamp);
    } else if (block.kind == CP2SemanticStateKind::kSlamLandmark) {
      mutate_scalar(block.landmark.anchor_timestamp);
    }
  }
  for (CP2ActiveStateType &type : snapshot.active_types) {
    if (type.tag == CP2ActiveStateTypeTag::kClone) {
      mutate_scalar(type.clone_timestamp);
    } else if (type.tag == CP2ActiveStateTypeTag::kSlamLandmark) {
      mutate_scalar(type.landmark.anchor_timestamp);
    }
    mutate_matrix(type.nominal);
    mutate_matrix(type.fej);
  }
  for (CP2FixedImuCalibration &calibration :
       snapshot.fixed_imu_calibrations) {
    mutate_matrix(calibration.nominal);
    mutate_matrix(calibration.fej);
  }
  mutate_matrix(snapshot.time_offset_nominal);
  mutate_matrix(snapshot.time_offset_fej);
  for (CP2CameraFixedCalibration &camera : snapshot.camera_calibrations) {
    mutate_matrix(camera.extrinsic_nominal);
    mutate_matrix(camera.extrinsic_fej);
    mutate_matrix(camera.intrinsic_nominal);
    mutate_matrix(camera.intrinsic_fej);
  }
  for (CP2CameraModelCache &cache : snapshot.camera_caches) {
    mutate_matrix(cache.calibration);
    mutate_matrix(cache.K);
    mutate_matrix(cache.D);
  }

  EXPECT_GT(mutations, UINT64_C(2000));
  EXPECT_TRUE(CP2CompositeStateAdapter::CanonicallyEqual(snapshot, baseline));
}

TEST(CP2CompositeStateCodec, IdentityMetadataShapeAndKeyMutationsAreDetected) {
  CP2CompositeStateSnapshot baseline =
      full_snapshot(CP2StatePhase::kPhase0Prior);
  std::uint64_t mutations = 0U;
  const auto detected = [&](const CP2CompositeStateSnapshot &mutated) {
    expect_encode_detects_mutation(baseline, mutated);
    ++mutations;
  };

  // Phase is outside the payload by contract for the compatible 0/1 and 2/3
  // pairs, but an out-of-domain phase must still fail closed at encoding.
  {
    CP2CompositeStateSnapshot mutated = baseline;
    mutated.phase = static_cast<CP2StatePhase>(255U);
    detected(mutated);
  }

  for (std::size_t index = 0; index < baseline.semantic_blocks.size(); ++index) {
    SCOPED_TRACE(::testing::Message() << "semantic metadata index=" << index);
    {
      CP2CompositeStateSnapshot mutated = baseline;
      mutated.semantic_blocks[index].kind =
          static_cast<CP2SemanticStateKind>(255U);
      detected(mutated);
    }
    {
      CP2CompositeStateSnapshot mutated = baseline;
      ++mutated.semantic_blocks[index].covariance_id;
      detected(mutated);
    }
    {
      CP2CompositeStateSnapshot mutated = baseline;
      ++mutated.semantic_blocks[index].error_size;
      detected(mutated);
    }
    if (baseline.semantic_blocks[index].kind ==
        CP2SemanticStateKind::kSlamLandmark) {
      {
        CP2CompositeStateSnapshot mutated = baseline;
        ++mutated.semantic_blocks[index].landmark.feature_id;
        detected(mutated);
      }
      {
        CP2CompositeStateSnapshot mutated = baseline;
        mutated.semantic_blocks[index].landmark.representation =
            CP2LandmarkRepresentation::kUnknown;
        detected(mutated);
      }
      {
        CP2CompositeStateSnapshot mutated = baseline;
        ++mutated.semantic_blocks[index].landmark.anchor_camera_id;
        detected(mutated);
      }
    }
  }

  for (std::size_t index = 0; index < baseline.active_types.size(); ++index) {
    SCOPED_TRACE(::testing::Message() << "active metadata index=" << index);
    {
      CP2CompositeStateSnapshot mutated = baseline;
      mutated.active_types[index].tag =
          static_cast<CP2ActiveStateTypeTag>(255U);
      detected(mutated);
    }
    {
      CP2CompositeStateSnapshot mutated = baseline;
      ++mutated.active_types[index].covariance_id;
      detected(mutated);
    }
    {
      CP2CompositeStateSnapshot mutated = baseline;
      ++mutated.active_types[index].error_size;
      detected(mutated);
    }
    if (baseline.active_types[index].tag ==
        CP2ActiveStateTypeTag::kSlamLandmark) {
      {
        CP2CompositeStateSnapshot mutated = baseline;
        ++mutated.active_types[index].landmark.feature_id;
        detected(mutated);
      }
      {
        CP2CompositeStateSnapshot mutated = baseline;
        mutated.active_types[index].landmark.representation =
            CP2LandmarkRepresentation::kUnknown;
        detected(mutated);
      }
      {
        CP2CompositeStateSnapshot mutated = baseline;
        ++mutated.active_types[index].landmark.anchor_camera_id;
        detected(mutated);
      }
    }
  }

  for (std::size_t index = 0;
       index < baseline.fixed_imu_calibrations.size(); ++index) {
    SCOPED_TRACE(::testing::Message() << "fixed IMU tag index=" << index);
    CP2CompositeStateSnapshot mutated = baseline;
    mutated.fixed_imu_calibrations[index].tag =
        static_cast<CP2FixedImuCalibrationTag>(255U);
    detected(mutated);
  }
  for (std::size_t index = 0; index < baseline.camera_calibrations.size();
       ++index) {
    SCOPED_TRACE(::testing::Message() << "camera calibration ID index=" << index);
    CP2CompositeStateSnapshot mutated = baseline;
    ++mutated.camera_calibrations[index].camera_id;
    detected(mutated);
  }
  for (std::size_t index = 0; index < baseline.camera_caches.size(); ++index) {
    SCOPED_TRACE(::testing::Message() << "camera cache metadata index=" << index);
    {
      CP2CompositeStateSnapshot mutated = baseline;
      ++mutated.camera_caches[index].camera_id;
      detected(mutated);
    }
    {
      CP2CompositeStateSnapshot mutated = baseline;
      ++mutated.camera_caches[index].width;
      detected(mutated);
    }
    {
      CP2CompositeStateSnapshot mutated = baseline;
      ++mutated.camera_caches[index].height;
      detected(mutated);
    }
  }

  const std::size_t matrix_count = all_matrix_fields(baseline).size();
  EXPECT_EQ(matrix_count, 45U);
  for (std::size_t index = 0; index < matrix_count; ++index) {
    SCOPED_TRACE(::testing::Message() << "matrix shape index=" << index);
    {
      CP2CompositeStateSnapshot mutated = baseline;
      Eigen::MatrixXd &matrix = *all_matrix_fields(mutated).at(index);
      ASSERT_GT(matrix.rows(), 0);
      matrix.conservativeResize(matrix.rows() - 1, matrix.cols());
      detected(mutated);
    }
    {
      CP2CompositeStateSnapshot mutated = baseline;
      Eigen::MatrixXd &matrix = *all_matrix_fields(mutated).at(index);
      ASSERT_GT(matrix.cols(), 0);
      matrix.conservativeResize(matrix.rows(), matrix.cols() - 1);
      detected(mutated);
    }
  }

  // Each of the five variable-length lists protects both its count and order.
  {
    CP2CompositeStateSnapshot mutated = baseline;
    mutated.semantic_blocks.pop_back();
    detected(mutated);
    mutated = baseline;
    std::swap(mutated.semantic_blocks[0], mutated.semantic_blocks[1]);
    detected(mutated);
  }
  {
    CP2CompositeStateSnapshot mutated = baseline;
    mutated.active_types.pop_back();
    detected(mutated);
    mutated = baseline;
    std::swap(mutated.active_types[0], mutated.active_types[1]);
    detected(mutated);
  }
  {
    CP2CompositeStateSnapshot mutated = baseline;
    mutated.fixed_imu_calibrations.pop_back();
    detected(mutated);
    mutated = baseline;
    std::swap(mutated.fixed_imu_calibrations[0],
              mutated.fixed_imu_calibrations[1]);
    detected(mutated);
  }
  {
    CP2CompositeStateSnapshot mutated = baseline;
    mutated.camera_calibrations.pop_back();
    detected(mutated);
    mutated = baseline;
    std::swap(mutated.camera_calibrations[0],
              mutated.camera_calibrations[1]);
    detected(mutated);
  }
  {
    CP2CompositeStateSnapshot mutated = baseline;
    mutated.camera_caches.pop_back();
    detected(mutated);
    mutated = baseline;
    std::swap(mutated.camera_caches[0], mutated.camera_caches[1]);
    detected(mutated);
  }

  // 122 enum/integer roles, 90 row/column shape roles, and ten list
  // count/order roles are each changed in isolation.
  EXPECT_EQ(mutations, UINT64_C(222));
}

TEST(CP2CompositeStateValidation, ExactPartitionsAndCloneIdentityFailClosed) {
  const CP2CompositeStateSnapshot baseline =
      full_snapshot(CP2StatePhase::kPhase0Prior);
  ASSERT_EQ(CP2CompositeStateAdapter::Validate(baseline),
            CP2CompositeStateStatus::kAccepted);

  CP2CompositeStateSnapshot malformed = baseline;
  malformed.covariance.conservativeResize(43, 42);
  EXPECT_EQ(CP2CompositeStateAdapter::Validate(malformed),
            CP2CompositeStateStatus::kInvalidShape);
  malformed = baseline;
  ++malformed.active_types.at(1).covariance_id;
  EXPECT_EQ(CP2CompositeStateAdapter::Validate(malformed),
            CP2CompositeStateStatus::kInvalidActivePartition);
  malformed = baseline;
  std::swap(malformed.active_types.at(1), malformed.active_types.at(2));
  EXPECT_EQ(CP2CompositeStateAdapter::Validate(malformed),
            CP2CompositeStateStatus::kInvalidActivePartition);
  malformed = baseline;
  ++malformed.semantic_blocks.at(6).covariance_id;
  EXPECT_EQ(CP2CompositeStateAdapter::Validate(malformed),
            CP2CompositeStateStatus::kInvalidSemanticPartition);
  malformed = baseline;
  std::swap(malformed.semantic_blocks.at(5), malformed.semantic_blocks.at(6));
  EXPECT_EQ(CP2CompositeStateAdapter::Validate(malformed),
            CP2CompositeStateStatus::kInvalidSemanticPartition);

  malformed = baseline;
  malformed.active_types.at(2).clone_timestamp =
      malformed.active_types.at(1).clone_timestamp;
  malformed.semantic_blocks.at(7).clone_timestamp =
      malformed.active_types.at(1).clone_timestamp;
  malformed.semantic_blocks.at(8).clone_timestamp =
      malformed.active_types.at(1).clone_timestamp;
  EXPECT_EQ(CP2CompositeStateAdapter::Validate(malformed),
            CP2CompositeStateStatus::kInvalidIdentity);

  malformed = baseline;
  malformed.active_types.at(1).clone_timestamp = -0.0;
  malformed.semantic_blocks.at(5).clone_timestamp = -0.0;
  malformed.semantic_blocks.at(6).clone_timestamp = -0.0;
  malformed.active_types.at(2).clone_timestamp = +0.0;
  malformed.semantic_blocks.at(7).clone_timestamp = +0.0;
  malformed.semantic_blocks.at(8).clone_timestamp = +0.0;
  EXPECT_EQ(CP2CompositeStateAdapter::Validate(malformed),
            CP2CompositeStateStatus::kInvalidIdentity);

  malformed = baseline;
  malformed.semantic_blocks.at(7).clone_timestamp =
      std::nextafter(malformed.semantic_blocks.at(7).clone_timestamp,
                     std::numeric_limits<double>::infinity());
  EXPECT_EQ(CP2CompositeStateAdapter::Validate(malformed),
            CP2CompositeStateStatus::kInvalidSemanticPartition);
}

TEST(CP2CompositeStateValidation, EveryLandmarkRepresentationIdentityRuleIsExact) {
  const CP2CompositeStateSnapshot baseline =
      full_snapshot(CP2StatePhase::kPhase0Prior);
  ASSERT_EQ(CP2CompositeStateAdapter::Validate(baseline),
            CP2CompositeStateStatus::kAccepted);

  for (std::size_t index = 3U; index < baseline.active_types.size(); ++index) {
    SCOPED_TRACE(::testing::Message() << "active_landmark=" << index);
    CP2CompositeStateSnapshot malformed = baseline;
    const std::size_t block_index = index + 6U;
    if (index == 3U || index == 4U) {
      malformed.active_types[index].landmark.anchor_camera_id = 0;
      malformed.semantic_blocks[block_index].landmark.anchor_camera_id = 0;
    } else {
      malformed.active_types[index].landmark.anchor_camera_id = -1;
      malformed.semantic_blocks[block_index].landmark.anchor_camera_id = -1;
    }
    EXPECT_EQ(CP2CompositeStateAdapter::Validate(malformed),
              CP2CompositeStateStatus::kInvalidIdentity);
  }

  CP2CompositeStateSnapshot malformed = baseline;
  malformed.active_types.at(3).landmark.anchor_timestamp = -0.0;
  malformed.semantic_blocks.at(9).landmark.anchor_timestamp = -0.0;
  EXPECT_EQ(CP2CompositeStateAdapter::Validate(malformed),
            CP2CompositeStateStatus::kInvalidIdentity);

  malformed = baseline;
  const double near_anchor = std::nextafter(
      1.25, std::numeric_limits<double>::infinity());
  malformed.active_types.at(5).landmark.anchor_timestamp = near_anchor;
  malformed.semantic_blocks.at(11).landmark.anchor_timestamp = near_anchor;
  EXPECT_EQ(CP2CompositeStateAdapter::Validate(malformed),
            CP2CompositeStateStatus::kInvalidIdentity);

  malformed = baseline;
  malformed.active_types.at(8).error_size = 3U;
  malformed.active_types.at(8).nominal.conservativeResize(3, 1);
  malformed.active_types.at(8).fej.conservativeResize(3, 1);
  malformed.semantic_blocks.at(14).error_size = 3U;
  EXPECT_NE(CP2CompositeStateAdapter::Validate(malformed),
            CP2CompositeStateStatus::kAccepted);

  malformed = baseline;
  malformed.active_types.at(7).landmark.feature_id =
      malformed.active_types.at(6).landmark.feature_id;
  malformed.semantic_blocks.at(13).landmark.feature_id =
      malformed.semantic_blocks.at(12).landmark.feature_id;
  EXPECT_EQ(CP2CompositeStateAdapter::Validate(malformed),
            CP2CompositeStateStatus::kInvalidIdentity);

  malformed = baseline;
  malformed.active_types.at(6).landmark.representation =
      CP2LandmarkRepresentation::kUnknown;
  malformed.semantic_blocks.at(12).landmark.representation =
      CP2LandmarkRepresentation::kUnknown;
  EXPECT_EQ(CP2CompositeStateAdapter::Validate(malformed),
            CP2CompositeStateStatus::kInvalidIdentity);
}

TEST(CP2CompositeStateValidation, QuaternionSquaredNormBoundaryIsRoleComplete) {
  const double epsilon = std::numeric_limits<double>::epsilon();
  Eigen::Vector4d equality;
  equality << 0.0, 0.0, 0.0, 1.0 + 16.0 * epsilon;
  Eigen::Vector4d next_error;
  next_error << std::ldexp(1.0, -26), 0.0, 0.0, 1.0 + 16.0 * epsilon;
  ASSERT_EQ(binary64_bits(equality.squaredNorm()),
            binary64_bits(1.0 + 32.0 * epsilon));
  ASSERT_EQ(binary64_bits(next_error.squaredNorm()),
            binary64_bits(std::nextafter(1.0 + 32.0 * epsilon,
                                         std::numeric_limits<double>::infinity())));

  CP2CompositeStateSnapshot role_inventory =
      full_snapshot(CP2StatePhase::kPhase0Prior);
  const std::size_t role_count = quaternion_fields(role_inventory).size();
  ASSERT_EQ(role_count, 14U);
  for (std::size_t role = 0; role < role_count; ++role) {
    SCOPED_TRACE(::testing::Message() << "quaternion_role=" << role);
    CP2CompositeStateSnapshot at_equality =
        full_snapshot(CP2StatePhase::kPhase0Prior);
    set_quaternion(*quaternion_fields(at_equality).at(role), equality);
    EXPECT_EQ(CP2CompositeStateAdapter::Validate(at_equality),
              CP2CompositeStateStatus::kAccepted);

    CP2CompositeStateSnapshot past_boundary =
        full_snapshot(CP2StatePhase::kPhase0Prior);
    set_quaternion(*quaternion_fields(past_boundary).at(role), next_error);
    EXPECT_EQ(CP2CompositeStateAdapter::Validate(past_boundary),
              CP2CompositeStateStatus::kInvalidQuaternion);
  }
}

TEST(CP2CompositeStateCodec, NonfiniteRolesRoundTripLosslesslyButNeverValidate) {
  using Mutator = std::function<void(CP2CompositeStateSnapshot &, double)>;
  std::vector<Mutator> roles;
  roles.emplace_back(
      [](CP2CompositeStateSnapshot &s, double v) { s.timestamp = v; });
  roles.emplace_back([](CP2CompositeStateSnapshot &s, double v) {
    s.covariance(0, 0) = v;
  });
  for (std::size_t index = 5U; index < 9U; ++index) {
    roles.emplace_back([index](CP2CompositeStateSnapshot &s, double v) {
      s.semantic_blocks.at(index).clone_timestamp = v;
    });
  }
  for (std::size_t index = 9U; index < 15U; ++index) {
    roles.emplace_back([index](CP2CompositeStateSnapshot &s, double v) {
      s.semantic_blocks.at(index).landmark.anchor_timestamp = v;
    });
  }
  for (std::size_t index = 1U; index < 3U; ++index) {
    roles.emplace_back([index](CP2CompositeStateSnapshot &s, double v) {
      s.active_types.at(index).clone_timestamp = v;
    });
  }
  for (std::size_t index = 3U; index < 9U; ++index) {
    roles.emplace_back([index](CP2CompositeStateSnapshot &s, double v) {
      s.active_types.at(index).landmark.anchor_timestamp = v;
    });
  }
  for (std::size_t index = 0U; index < 9U; ++index) {
    roles.emplace_back([index](CP2CompositeStateSnapshot &s, double v) {
      s.active_types.at(index).nominal(0, 0) = v;
    });
    roles.emplace_back([index](CP2CompositeStateSnapshot &s, double v) {
      s.active_types.at(index).fej(0, 0) = v;
    });
  }
  for (std::size_t index = 0U; index < 5U; ++index) {
    roles.emplace_back([index](CP2CompositeStateSnapshot &s, double v) {
      s.fixed_imu_calibrations.at(index).nominal(0, 0) = v;
    });
    roles.emplace_back([index](CP2CompositeStateSnapshot &s, double v) {
      s.fixed_imu_calibrations.at(index).fej(0, 0) = v;
    });
  }
  roles.emplace_back([](CP2CompositeStateSnapshot &s, double v) {
    s.time_offset_nominal(0, 0) = v;
  });
  roles.emplace_back([](CP2CompositeStateSnapshot &s, double v) {
    s.time_offset_fej(0, 0) = v;
  });
  for (std::size_t index = 0U; index < 2U; ++index) {
    roles.emplace_back([index](CP2CompositeStateSnapshot &s, double v) {
      s.camera_calibrations.at(index).extrinsic_nominal(0, 0) = v;
    });
    roles.emplace_back([index](CP2CompositeStateSnapshot &s, double v) {
      s.camera_calibrations.at(index).extrinsic_fej(0, 0) = v;
    });
    roles.emplace_back([index](CP2CompositeStateSnapshot &s, double v) {
      s.camera_calibrations.at(index).intrinsic_nominal(0, 0) = v;
    });
    roles.emplace_back([index](CP2CompositeStateSnapshot &s, double v) {
      s.camera_calibrations.at(index).intrinsic_fej(0, 0) = v;
    });
    roles.emplace_back([index](CP2CompositeStateSnapshot &s, double v) {
      s.camera_caches.at(index).calibration(0, 0) = v;
    });
    roles.emplace_back([index](CP2CompositeStateSnapshot &s, double v) {
      s.camera_caches.at(index).K(0, 0) = v;
    });
    roles.emplace_back([index](CP2CompositeStateSnapshot &s, double v) {
      s.camera_caches.at(index).D(0, 0) = v;
    });
  }
  ASSERT_EQ(roles.size(), 64U);
  const std::vector<double> exceptional = {
      std::numeric_limits<double>::infinity(),
      -std::numeric_limits<double>::infinity(),
      binary64_from_bits(UINT64_C(0x7ff80000000000a5)),
      binary64_from_bits(UINT64_C(0x7ff0000000000001)),
  };

  for (std::size_t role = 0; role < roles.size(); ++role) {
    for (double value : exceptional) {
      SCOPED_TRACE(::testing::Message()
                   << "nonfinite_role=" << role << " bits=" << binary64_bits(value));
      CP2CompositeStateSnapshot snapshot =
          full_snapshot(CP2StatePhase::kPhase0Prior);
      roles[role](snapshot, value);
      const std::vector<std::uint8_t> payload =
          CP2StateTraceCodec::EncodeSnapshotPayload(snapshot);
      const CP2CompositeStateSnapshot decoded =
          CP2StateTraceCodec::DecodeSnapshotPayload(
              payload, CP2StatePhase::kPhase0Prior);
      EXPECT_TRUE(CP2CompositeStateAdapter::CanonicallyEqual(snapshot, decoded));
      EXPECT_EQ(CP2StateTraceCodec::EncodeSnapshotPayload(decoded), payload);
      EXPECT_NE(CP2CompositeStateAdapter::Validate(decoded),
                CP2CompositeStateStatus::kAccepted);
    }
  }

  // Protect the finite encodability cases that ordinary floating-point
  // equality would collapse or overlook: both zero signs and both minimum
  // subnormal signs must survive the complete value-object round trip.
  const std::vector<double> finite_specials = {
      +0.0, -0.0, std::numeric_limits<double>::denorm_min(),
      -std::numeric_limits<double>::denorm_min(),
  };
  for (double value : finite_specials) {
    SCOPED_TRACE(::testing::Message() << "finite-special bits="
                                      << binary64_bits(value));
    CP2CompositeStateSnapshot snapshot =
        full_snapshot(CP2StatePhase::kPhase0Prior);
    snapshot.camera_caches.at(0).D(0, 0) = value;
    const std::vector<std::uint8_t> payload =
        CP2StateTraceCodec::EncodeSnapshotPayload(snapshot);
    const CP2CompositeStateSnapshot decoded =
        CP2StateTraceCodec::DecodeSnapshotPayload(
            payload, CP2StatePhase::kPhase0Prior);
    EXPECT_EQ(binary64_bits(decoded.camera_caches.at(0).D(0, 0)),
              binary64_bits(value));
    EXPECT_TRUE(CP2CompositeStateAdapter::CanonicallyEqual(snapshot, decoded));
    EXPECT_EQ(CP2CompositeStateAdapter::Validate(decoded),
              CP2CompositeStateStatus::kAccepted);
  }
}

TEST(CP2StateFileCodec, LegalPhasePopulationsRoundTripAndCorruptionFailsClosed) {
  const CP2InvocationTraceIdentity first_identity{1U, 2U, 3U};
  const CP2InvocationTraceIdentity second_identity{1U, 3U, 4U};
  const auto frame = [](const CP2InvocationTraceIdentity &identity,
                        CP2StatePhase phase) {
    CP2StateTraceFrame result;
    result.invocation = identity;
    result.phase = phase;
    result.snapshot = full_snapshot(phase);
    return result;
  };

  const std::vector<CP2StateTraceFrame> legal = {
      frame(first_identity, CP2StatePhase::kPhase0Prior),
      frame(first_identity, CP2StatePhase::kPhase1Precommit),
      frame(second_identity, CP2StatePhase::kPhase0Prior),
      frame(second_identity, CP2StatePhase::kPhase1Precommit),
      frame(second_identity, CP2StatePhase::kPhase2ExpectedPostcommit),
      frame(second_identity, CP2StatePhase::kPhase3LivePostcommit),
  };
  const ov_msckf::CP2EncodedStateFile encoded =
      CP2StateTraceCodec::EncodeStateFile(legal);
  ASSERT_EQ(encoded.payloads.size(), legal.size());
  const std::string state_magic("SchurVIO-CP2-state-file-v1\n\0\0\0\0\0", 32U);
  ASSERT_GE(encoded.bytes.size(), state_magic.size());
  EXPECT_TRUE(std::equal(state_magic.begin(), state_magic.end(),
                         encoded.bytes.begin()));
  EXPECT_EQ(encoded.bytes.size(), 116286U);
  const std::uint64_t expected_offsets[] = {
      72U, 19447U, 38822U, 58197U, 77572U, 96949U,
  };
  const std::uint64_t expected_lengths[] = {
      19335U, 19335U, 19335U, 19335U, 19337U, 19337U,
  };
  for (std::size_t index = 0; index < encoded.payloads.size(); ++index) {
    EXPECT_EQ(encoded.payloads[index].offset, expected_offsets[index]);
    EXPECT_EQ(encoded.payloads[index].length, expected_lengths[index]);
    EXPECT_EQ(encoded.payloads[index].sha256,
              sha256_hex(CP2StateTraceCodec::EncodeSnapshotPayload(
                  legal[index].snapshot)));
  }
  const std::vector<CP2StateTraceFrame> decoded =
      CP2StateTraceCodec::DecodeStateFile(encoded.bytes);
  ASSERT_EQ(decoded.size(), legal.size());
  for (std::size_t index = 0; index < legal.size(); ++index) {
    EXPECT_EQ(decoded[index].invocation, legal[index].invocation);
    EXPECT_EQ(decoded[index].phase, legal[index].phase);
    EXPECT_TRUE(CP2CompositeStateAdapter::CanonicallyEqual(
        decoded[index].snapshot, legal[index].snapshot));
    EXPECT_EQ(decoded[index].payload.sha256, encoded.payloads[index].sha256);
  }
  EXPECT_EQ(CP2StateTraceCodec::EncodeStateFile(decoded).bytes, encoded.bytes);

  const auto expect_state_identity_detected =
      [&](const std::vector<CP2StateTraceFrame> &mutated) {
        try {
          EXPECT_NE(CP2StateTraceCodec::EncodeStateFile(mutated).bytes,
                    encoded.bytes);
        } catch (const ov_msckf::CP2TraceCodecError &) {
          SUCCEED();
        }
      };
  for (std::size_t index = 0; index < legal.size(); ++index) {
    SCOPED_TRACE(::testing::Message() << "state-frame identity index=" << index);
    {
      std::vector<CP2StateTraceFrame> mutated = legal;
      ++mutated[index].invocation.sequence_index;
      expect_state_identity_detected(mutated);
    }
    {
      std::vector<CP2StateTraceFrame> mutated = legal;
      ++mutated[index].invocation.pair_index;
      expect_state_identity_detected(mutated);
    }
    {
      std::vector<CP2StateTraceFrame> mutated = legal;
      ++mutated[index].invocation.invocation_id;
      expect_state_identity_detected(mutated);
    }
    {
      std::vector<CP2StateTraceFrame> mutated = legal;
      mutated[index].phase = static_cast<CP2StatePhase>(255U);
      mutated[index].snapshot.phase = static_cast<CP2StatePhase>(255U);
      expect_state_identity_detected(mutated);
    }
  }

  // Pairwise equality is deliberately not a codec precondition: a complete
  // phase-1 mismatch and complete invalid phase 3 are retained so the
  // higher-level classifier can emit the approved failed evidence.
  std::vector<CP2StateTraceFrame> retained_failure = legal;
  retained_failure.at(1).snapshot.timestamp = std::nextafter(
      retained_failure.at(1).snapshot.timestamp,
      std::numeric_limits<double>::infinity());
  retained_failure.at(5).snapshot.covariance(0, 0) =
      binary64_from_bits(UINT64_C(0x7ff80000000000a5));
  const ov_msckf::CP2EncodedStateFile retained_encoded =
      CP2StateTraceCodec::EncodeStateFile(retained_failure);
  const std::vector<CP2StateTraceFrame> retained_decoded =
      CP2StateTraceCodec::DecodeStateFile(retained_encoded.bytes);
  ASSERT_EQ(retained_decoded.size(), retained_failure.size());
  EXPECT_FALSE(CP2CompositeStateAdapter::CanonicallyEqual(
      retained_decoded.at(0).snapshot, retained_decoded.at(1).snapshot));
  EXPECT_EQ(CP2CompositeStateAdapter::Validate(retained_decoded.at(5).snapshot),
            CP2CompositeStateStatus::kNonfinite);

  std::vector<CP2StateTraceFrame> malformed = legal;
  malformed.erase(malformed.begin() + 1);
  EXPECT_THROW(CP2StateTraceCodec::EncodeStateFile(malformed),
               ov_msckf::CP2TraceCodecError);
  malformed = legal;
  malformed.erase(malformed.begin() + 4);
  EXPECT_THROW(CP2StateTraceCodec::EncodeStateFile(malformed),
               ov_msckf::CP2TraceCodecError);
  malformed = legal;
  malformed.insert(malformed.begin() + 1, malformed.front());
  EXPECT_THROW(CP2StateTraceCodec::EncodeStateFile(malformed),
               ov_msckf::CP2TraceCodecError);
  malformed = legal;
  std::swap(malformed[0], malformed[1]);
  EXPECT_THROW(CP2StateTraceCodec::EncodeStateFile(malformed),
               ov_msckf::CP2TraceCodecError);

  std::vector<std::uint8_t> bad_magic = encoded.bytes;
  bad_magic[0] ^= 1U;
  EXPECT_THROW(CP2StateTraceCodec::DecodeStateFile(bad_magic),
               ov_msckf::CP2TraceCodecError);
  std::vector<std::uint8_t> bad_phase = encoded.bytes;
  bad_phase[32] = 4U;
  EXPECT_THROW(CP2StateTraceCodec::DecodeStateFile(bad_phase),
               ov_msckf::CP2TraceCodecError);
  for (std::size_t reserved_index = 0; reserved_index < 7U;
       ++reserved_index) {
    SCOPED_TRACE(::testing::Message()
                 << "state-frame reserved byte=" << reserved_index);
    std::vector<std::uint8_t> bad_reserved = encoded.bytes;
    bad_reserved[33U + reserved_index] = 1U;
    EXPECT_THROW(CP2StateTraceCodec::DecodeStateFile(bad_reserved),
                 ov_msckf::CP2TraceCodecError);
  }
  std::vector<std::uint8_t> duplicate_decoded_key = encoded.bytes;
  duplicate_decoded_key[19407U] = 0U;
  EXPECT_THROW(CP2StateTraceCodec::DecodeStateFile(duplicate_decoded_key),
               ov_msckf::CP2TraceCodecError);
  std::vector<std::uint8_t> truncated = encoded.bytes;
  truncated.pop_back();
  EXPECT_THROW(CP2StateTraceCodec::DecodeStateFile(truncated),
               ov_msckf::CP2TraceCodecError);
  std::vector<std::uint8_t> trailing = encoded.bytes;
  trailing.push_back(0U);
  EXPECT_THROW(CP2StateTraceCodec::DecodeStateFile(trailing),
               ov_msckf::CP2TraceCodecError);
  std::vector<std::uint8_t> hostile_length = encoded.bytes;
  for (std::size_t index = 64U; index < 72U; ++index) {
    hostile_length[index] = 0xffU;
  }
  EXPECT_THROW(CP2StateTraceCodec::DecodeStateFile(hostile_length),
               ov_msckf::CP2TraceCodecError);

  std::vector<std::uint8_t> wrong_payload_domain = encoded.bytes;
  wrong_payload_domain[72U] ^= 1U;
  EXPECT_THROW(CP2StateTraceCodec::DecodeStateFile(wrong_payload_domain),
               ov_msckf::CP2TraceCodecError);

  const std::vector<std::uint8_t> payload =
      CP2StateTraceCodec::EncodeSnapshotPayload(legal.front().snapshot);
  std::vector<std::uint8_t> hostile_shape = payload;
  // prior-domain(31), timestamp(8), then covariance row count.
  store_u64(hostile_shape, 39U, UINT64_MAX);
  EXPECT_THROW(CP2StateTraceCodec::DecodeSnapshotPayload(
                   hostile_shape, CP2StatePhase::kPhase0Prior),
               ov_msckf::CP2TraceCodecError);

  std::vector<std::uint8_t> hostile_semantic_count = payload;
  const std::size_t semantic_count_offset =
      31U + 8U + 16U + 43U * 43U * sizeof(double);
  store_u64(hostile_semantic_count, semantic_count_offset, UINT64_MAX);
  EXPECT_THROW(CP2StateTraceCodec::DecodeSnapshotPayload(
                   hostile_semantic_count, CP2StatePhase::kPhase0Prior),
               ov_msckf::CP2TraceCodecError);

  std::vector<std::uint8_t> unknown_semantic_kind = payload;
  const std::size_t first_kind_byte = semantic_count_offset + 8U + 8U;
  ASSERT_LT(first_kind_byte, unknown_semantic_kind.size());
  unknown_semantic_kind[first_kind_byte] ^= 1U;
  EXPECT_THROW(CP2StateTraceCodec::DecodeSnapshotPayload(
                   unknown_semantic_kind, CP2StatePhase::kPhase0Prior),
               ov_msckf::CP2TraceCodecError);

  ov_msckf::CP2TraceDecodeLimits coefficient_limit;
  coefficient_limit.maximum_matrix_coefficients = 1U;
  coefficient_limit.maximum_total_matrix_coefficients = 1U;
  EXPECT_THROW(CP2StateTraceCodec::DecodeSnapshotPayload(
                   payload, CP2StatePhase::kPhase0Prior, coefficient_limit),
               ov_msckf::CP2TraceCodecError);
  ov_msckf::CP2TraceDecodeLimits frame_limit;
  frame_limit.maximum_frames = 1U;
  EXPECT_THROW(CP2StateTraceCodec::DecodeStateFile(encoded.bytes, frame_limit),
               ov_msckf::CP2TraceCodecError);
}

TEST(CP2CompositeLiveCapture,
     ProductionStateProjectsOneOwningPriorAndIgnoresAddressesAndMapInsertion) {
  LiveCompositeFixture first = live_fixture(false);
  LiveCompositeFixture shuffled = live_fixture(true);
  CP2CompositeStateCapture first_capture;
  CP2CompositeStateCapture shuffled_capture;
  ASSERT_EQ(CP2CompositeStateAdapter::Capture(
                first.state, CP2StatePhase::kPhase0Prior, first_capture),
            CP2CompositeStateStatus::kAccepted);
  ASSERT_EQ(CP2CompositeStateAdapter::Capture(
                shuffled.state, CP2StatePhase::kPhase0Prior, shuffled_capture),
            CP2CompositeStateStatus::kAccepted);
  ASSERT_EQ(CP2CompositeStateAdapter::Validate(first_capture.snapshot),
            CP2CompositeStateStatus::kAccepted);
  ASSERT_EQ(CP2CompositeStateAdapter::Validate(shuffled_capture.snapshot),
            CP2CompositeStateStatus::kAccepted);

  EXPECT_TRUE(CP2CompositeStateAdapter::CanonicallyEqual(
      first_capture.snapshot, shuffled_capture.snapshot));
  EXPECT_EQ(CP2StateTraceCodec::EncodeSnapshotPayload(first_capture.snapshot),
            CP2StateTraceCodec::EncodeSnapshotPayload(shuffled_capture.snapshot));
  EXPECT_TRUE(CP2CompositeStateAdapter::PointerGraphMatches(
      first.state, first_capture.pointer_graph));
  EXPECT_TRUE(CP2CompositeStateAdapter::PointerGraphMatches(
      shuffled.state, shuffled_capture.pointer_graph));

  const std::vector<std::uint8_t> captured_prior_bytes =
      CP2StateTraceCodec::EncodeSnapshotPayload(first_capture.snapshot);
  Eigen::MatrixXd changed_live_imu = first.state->_imu->value();
  changed_live_imu(4, 0) += 100.0;
  first.state->_imu->set_value(changed_live_imu);
  first.state->_imu->set_local_id(17);
  first.state->_timestamp = 99.0;
  ASSERT_FALSE(matrix_bits_equal(first.state->_imu->value(),
                                 first_capture.snapshot.active_types.at(0).nominal));
  ASSERT_NE(first.state->_imu->id(),
            static_cast<int>(
                first_capture.snapshot.active_types.at(0).covariance_id));
  ASSERT_NE(binary64_bits(first.state->_timestamp),
            binary64_bits(first_capture.snapshot.timestamp));
  EXPECT_EQ(CP2StateTraceCodec::EncodeSnapshotPayload(first_capture.snapshot),
            captured_prior_bytes);

  // ProjectPreview consumes only the captured owning phase-0 value. The live
  // State above is now deliberately inconsistent with it, so these equalities
  // protect the absence of any hidden live-state dependency.
  ov_msckf::MSCKFUpdatePreviewSnapshot preview;
  ASSERT_EQ(CP2CompositeStateAdapter::ProjectPreview(first_capture.snapshot, preview),
            CP2CompositeStateStatus::kAccepted);
  EXPECT_TRUE(matrix_bits_equal(preview.covariance,
                                first_capture.snapshot.covariance));
  ASSERT_EQ(preview.state_blocks.size(),
            first_capture.snapshot.active_types.size());
  for (std::size_t index = 0; index < preview.state_blocks.size(); ++index) {
    const CP2ActiveStateType &active = first_capture.snapshot.active_types[index];
    EXPECT_EQ(preview.state_blocks[index].covariance_id,
              static_cast<Eigen::Index>(active.covariance_id));
    EXPECT_EQ(preview.state_blocks[index].size,
              static_cast<Eigen::Index>(active.error_size));
    EXPECT_EQ(preview.state_blocks[index].offset,
              static_cast<Eigen::Index>(active.covariance_id));
  }
  for (CP2StatePhase forbidden_phase : {
           CP2StatePhase::kPhase1Precommit,
           CP2StatePhase::kPhase2ExpectedPostcommit,
           CP2StatePhase::kPhase3LivePostcommit}) {
    CP2CompositeStateSnapshot wrong_phase = first_capture.snapshot;
    wrong_phase.phase = forbidden_phase;
    ov_msckf::MSCKFUpdatePreviewSnapshot untouched = preview;
    EXPECT_EQ(CP2CompositeStateAdapter::ProjectPreview(wrong_phase, untouched),
              CP2CompositeStateStatus::kInvalidPhase);
    EXPECT_TRUE(matrix_bits_equal(untouched.covariance, preview.covariance));
    ASSERT_EQ(untouched.state_blocks.size(), preview.state_blocks.size());
    for (std::size_t index = 0; index < preview.state_blocks.size(); ++index) {
      EXPECT_EQ(untouched.state_blocks[index].covariance_id,
                preview.state_blocks[index].covariance_id);
      EXPECT_EQ(untouched.state_blocks[index].size,
                preview.state_blocks[index].size);
      EXPECT_EQ(untouched.state_blocks[index].offset,
                preview.state_blocks[index].offset);
    }
  }

  ASSERT_EQ(first_capture.snapshot.active_types.size(), 5U);
  EXPECT_EQ(first_capture.snapshot.active_types.at(0).tag,
            CP2ActiveStateTypeTag::kImu);
  EXPECT_EQ(first_capture.snapshot.active_types.at(1).clone_timestamp, 1.25);
  EXPECT_EQ(first_capture.snapshot.active_types.at(2).clone_timestamp, 2.5);
  EXPECT_EQ(first_capture.snapshot.active_types.at(3).landmark.feature_id, 501U);
  EXPECT_EQ(first_capture.snapshot.active_types.at(4).landmark.feature_id, 502U);
  EXPECT_EQ(first_capture.snapshot.active_types.at(4).landmark.representation,
            CP2LandmarkRepresentation::kAnchoredInverseDepthSingle);
  EXPECT_EQ(first_capture.snapshot.camera_caches.size(), 2U);
  EXPECT_TRUE(matrix_bits_equal(first_capture.snapshot.camera_caches.at(0).calibration,
                                first.state->_cam_intrinsics_cameras.at(0)->get_value()));
}

TEST(CP2CompositePointerGraph,
     Phase1MatchesAndEveryPointerAssociationMutationIsDetected) {
  LiveCompositeFixture fixture = live_fixture(false);
  CP2CompositeStateCapture phase0;
  CP2CompositeStateCapture phase1;
  ASSERT_EQ(CP2CompositeStateAdapter::Capture(
                fixture.state, CP2StatePhase::kPhase0Prior, phase0),
            CP2CompositeStateStatus::kAccepted);
  ASSERT_EQ(CP2CompositeStateAdapter::Capture(
                fixture.state, CP2StatePhase::kPhase1Precommit, phase1),
            CP2CompositeStateStatus::kAccepted);
  EXPECT_TRUE(CP2CompositeStateAdapter::CanonicallyEqual(phase0.snapshot,
                                                          phase1.snapshot));
  EXPECT_TRUE(CP2CompositeStateAdapter::PointerGraphMatches(
      fixture.state, phase0.pointer_graph));
  std::uint64_t pointer_mutations = 0U;
  const auto detected = [&](auto mutation) {
    expect_pointer_graph_mutation_detected(std::move(mutation));
    ++pointer_mutations;
  };
  const auto detected_after_prepare = [&](auto prepare, auto mutation) {
    expect_pointer_graph_mutation_detected(std::move(prepare),
                                           std::move(mutation));
    ++pointer_mutations;
  };

  // Every active-vector entry is independently protected, as are its order
  // and both removal and addition membership changes.
  ASSERT_EQ(fixture.top_level.size(), 5U);
  for (std::size_t index = 0; index < fixture.top_level.size(); ++index) {
    SCOPED_TRACE(::testing::Message() << "active-vector entry=" << index);
    detected([index](LiveCompositeFixture &changed) {
      active_variables(*changed.state).at(index).reset();
    });
  }
  detected([](LiveCompositeFixture &changed) {
    std::swap(active_variables(*changed.state).at(0),
              active_variables(*changed.state).at(1));
  });
  detected([](LiveCompositeFixture &changed) {
    active_variables(*changed.state).pop_back();
  });
  detected([](LiveCompositeFixture &changed) {
    active_variables(*changed.state).push_back(changed.state->_imu);
  });

  detected([](LiveCompositeFixture &changed) {
    changed.state->_imu = std::make_shared<ov_type::IMU>();
  });
  detected_after_prepare(
      [](LiveCompositeFixture &changed) { install_mutable_imu(changed, false); },
      [](LiveCompositeFixture &changed) {
        std::dynamic_pointer_cast<MutableIMU>(changed.state->_imu)
            ->replace_pose(std::make_shared<ov_type::PoseJPL>());
      });
  detected_after_prepare(
      [](LiveCompositeFixture &changed) { install_mutable_imu(changed, true); },
      [](LiveCompositeFixture &changed) {
        std::dynamic_pointer_cast<MutablePoseJPL>(changed.state->_imu->pose())
            ->replace_quaternion(std::make_shared<ov_type::JPLQuat>());
      });
  detected_after_prepare(
      [](LiveCompositeFixture &changed) { install_mutable_imu(changed, true); },
      [](LiveCompositeFixture &changed) {
        std::dynamic_pointer_cast<MutablePoseJPL>(changed.state->_imu->pose())
            ->replace_position(std::make_shared<ov_type::Vec>(3));
      });
  detected_after_prepare(
      [](LiveCompositeFixture &changed) { install_mutable_imu(changed, false); },
      [](LiveCompositeFixture &changed) {
        std::dynamic_pointer_cast<MutableIMU>(changed.state->_imu)
            ->replace_velocity(std::make_shared<ov_type::Vec>(3));
      });
  detected_after_prepare(
      [](LiveCompositeFixture &changed) { install_mutable_imu(changed, false); },
      [](LiveCompositeFixture &changed) {
        std::dynamic_pointer_cast<MutableIMU>(changed.state->_imu)
            ->replace_gyro_bias(std::make_shared<ov_type::Vec>(3));
      });
  detected_after_prepare(
      [](LiveCompositeFixture &changed) { install_mutable_imu(changed, false); },
      [](LiveCompositeFixture &changed) {
        std::dynamic_pointer_cast<MutableIMU>(changed.state->_imu)
            ->replace_accel_bias(std::make_shared<ov_type::Vec>(3));
      });

  const double clone_timestamps[] = {1.25, 2.5};
  for (double timestamp : clone_timestamps) {
    SCOPED_TRACE(::testing::Message() << "clone timestamp=" << timestamp);
    detected([timestamp](LiveCompositeFixture &changed) {
      const std::shared_ptr<ov_type::PoseJPL> pointer =
          changed.state->_clones_IMU.at(timestamp);
      changed.state->_clones_IMU.erase(timestamp);
      changed.state->_clones_IMU.emplace(timestamp + 0.125, pointer);
    });
    detected([timestamp](LiveCompositeFixture &changed) {
      changed.state->_clones_IMU.at(timestamp) =
          std::make_shared<ov_type::PoseJPL>();
    });
    detected_after_prepare(
        [timestamp](LiveCompositeFixture &changed) {
          install_mutable_clone(changed, timestamp);
        },
        [timestamp](LiveCompositeFixture &changed) {
          std::dynamic_pointer_cast<MutablePoseJPL>(
              changed.state->_clones_IMU.at(timestamp))
              ->replace_quaternion(std::make_shared<ov_type::JPLQuat>());
        });
    detected_after_prepare(
        [timestamp](LiveCompositeFixture &changed) {
          install_mutable_clone(changed, timestamp);
        },
        [timestamp](LiveCompositeFixture &changed) {
          std::dynamic_pointer_cast<MutablePoseJPL>(
              changed.state->_clones_IMU.at(timestamp))
              ->replace_position(std::make_shared<ov_type::Vec>(3));
        });
  }

  const std::size_t feature_ids[] = {501U, 502U};
  for (std::size_t feature_id : feature_ids) {
    SCOPED_TRACE(::testing::Message() << "SLAM feature=" << feature_id);
    detected([feature_id](LiveCompositeFixture &changed) {
      const std::shared_ptr<ov_type::Landmark> pointer =
          changed.state->_features_SLAM.at(feature_id);
      changed.state->_features_SLAM.erase(feature_id);
      changed.state->_features_SLAM.emplace(feature_id + 1000U, pointer);
    });
    detected([feature_id](LiveCompositeFixture &changed) {
      const int dimension =
          changed.state->_features_SLAM.at(feature_id)->size();
      changed.state->_features_SLAM.at(feature_id) =
          std::make_shared<ov_type::Landmark>(dimension);
    });
  }

  detected([](LiveCompositeFixture &changed) {
    changed.state->_calib_imu_dw = std::make_shared<ov_type::Vec>(6);
  });
  detected([](LiveCompositeFixture &changed) {
    changed.state->_calib_imu_da = std::make_shared<ov_type::Vec>(6);
  });
  detected([](LiveCompositeFixture &changed) {
    changed.state->_calib_imu_tg = std::make_shared<ov_type::Vec>(9);
  });
  detected([](LiveCompositeFixture &changed) {
    changed.state->_calib_imu_GYROtoIMU =
        std::make_shared<ov_type::JPLQuat>();
  });
  detected([](LiveCompositeFixture &changed) {
    changed.state->_calib_imu_ACCtoIMU =
        std::make_shared<ov_type::JPLQuat>();
  });
  detected([](LiveCompositeFixture &changed) {
    changed.state->_calib_dt_CAMtoIMU = std::make_shared<ov_type::Vec>(1);
  });

  for (std::size_t camera_id = 0; camera_id < 2U; ++camera_id) {
    SCOPED_TRACE(::testing::Message() << "camera=" << camera_id);
    detected([camera_id](LiveCompositeFixture &changed) {
      changed.state->_calib_IMUtoCAM.at(camera_id) =
          std::make_shared<ov_type::PoseJPL>();
    });
    detected([camera_id](LiveCompositeFixture &changed) {
      changed.state->_cam_intrinsics.at(camera_id) =
          std::make_shared<ov_type::Vec>(8);
    });
    detected([camera_id](LiveCompositeFixture &changed) {
      changed.state->_cam_intrinsics_cameras.at(camera_id) =
          std::make_shared<ov_core::CamRadtan>(752, 480);
    });
    detected([camera_id](LiveCompositeFixture &changed) {
      const std::shared_ptr<ov_type::PoseJPL> pointer =
          changed.state->_calib_IMUtoCAM.at(camera_id);
      changed.state->_calib_IMUtoCAM.erase(camera_id);
      changed.state->_calib_IMUtoCAM.emplace(camera_id + 10U, pointer);
    });
    detected([camera_id](LiveCompositeFixture &changed) {
      const std::shared_ptr<ov_type::Vec> pointer =
          changed.state->_cam_intrinsics.at(camera_id);
      changed.state->_cam_intrinsics.erase(camera_id);
      changed.state->_cam_intrinsics.emplace(camera_id + 10U, pointer);
    });
    detected([camera_id](LiveCompositeFixture &changed) {
      const std::shared_ptr<ov_core::CamBase> pointer =
          changed.state->_cam_intrinsics_cameras.at(camera_id);
      changed.state->_cam_intrinsics_cameras.erase(camera_id);
      changed.state->_cam_intrinsics_cameras.emplace(camera_id + 10U, pointer);
    });
  }

  EXPECT_EQ(pointer_mutations, UINT64_C(45));

  // A value change leaves the identity graph intact but is independently
  // caught by the phase-0/phase-1 canonical comparison.
  Eigen::MatrixXd changed_imu = fixture.state->_imu->value();
  changed_imu(4, 0) += 0.125;
  fixture.state->_imu->set_value(changed_imu);
  EXPECT_TRUE(CP2CompositeStateAdapter::PointerGraphMatches(
      fixture.state, phase0.pointer_graph));
  CP2CompositeStateCapture changed_phase1;
  ASSERT_EQ(CP2CompositeStateAdapter::Capture(
                fixture.state, CP2StatePhase::kPhase1Precommit, changed_phase1),
            CP2CompositeStateStatus::kAccepted);
  EXPECT_FALSE(CP2CompositeStateAdapter::CanonicallyEqual(
      phase0.snapshot, changed_phase1.snapshot));
}

TEST(CP2CompositeLiveCapture,
     ParentSubvariableInactiveCalibrationAndCameraInventoryFaultsAreRejected) {
  {
    LiveCompositeFixture malformed = live_fixture(false);
    const std::shared_ptr<MutableIMU> imu =
        install_mutable_imu(malformed, false);
    imu->force_parent_id_for_overflow_test(std::numeric_limits<int>::max());
    CP2CompositeStateCapture capture;
    EXPECT_EQ(CP2CompositeStateAdapter::Capture(
                  malformed.state, CP2StatePhase::kPhase0Prior, capture),
              CP2CompositeStateStatus::kIncompleteStructure);
  }
  {
    LiveCompositeFixture malformed = live_fixture(false);
    const std::shared_ptr<MutablePoseJPL> clone =
        install_mutable_clone(malformed, 1.25);
    clone->force_parent_id_for_overflow_test(std::numeric_limits<int>::max());
    CP2CompositeStateCapture capture;
    EXPECT_EQ(CP2CompositeStateAdapter::Capture(
                  malformed.state, CP2StatePhase::kPhase0Prior, capture),
              CP2CompositeStateStatus::kInvalidPointerGraph);
  }
  {
    LiveCompositeFixture malformed = live_fixture(false);
    malformed.state->_clones_IMU.at(1.25)->q()->set_value(unit_quaternion(1U));
    CP2CompositeStateCapture capture;
    EXPECT_EQ(CP2CompositeStateAdapter::Capture(
                  malformed.state, CP2StatePhase::kPhase0Prior, capture),
              CP2CompositeStateStatus::kInvalidPointerGraph);
  }
  {
    LiveCompositeFixture malformed = live_fixture(false);
    malformed.state->_imu->q()->set_value(unit_quaternion(0U));
    CP2CompositeStateCapture capture;
    EXPECT_EQ(CP2CompositeStateAdapter::Capture(
                  malformed.state, CP2StatePhase::kPhase0Prior, capture),
              CP2CompositeStateStatus::kIncompleteStructure);
  }
  {
    LiveCompositeFixture malformed = live_fixture(false);
    malformed.state->_calib_imu_dw->set_local_id(0);
    CP2CompositeStateCapture capture;
    EXPECT_EQ(CP2CompositeStateAdapter::Capture(
                  malformed.state, CP2StatePhase::kPhase0Prior, capture),
              CP2CompositeStateStatus::kIncompleteStructure);
  }
  {
    LiveCompositeFixture malformed = live_fixture(false);
    malformed.state->_cam_intrinsics_cameras.erase(1U);
    CP2CompositeStateCapture capture;
    EXPECT_EQ(CP2CompositeStateAdapter::Capture(
                  malformed.state, CP2StatePhase::kPhase0Prior, capture),
              CP2CompositeStateStatus::kIncompleteStructure);
  }
  {
    LiveCompositeFixture malformed = live_fixture(false);
    const std::shared_ptr<ov_core::CamBase> cache =
        malformed.state->_cam_intrinsics_cameras.at(1U);
    malformed.state->_cam_intrinsics_cameras.erase(1U);
    malformed.state->_cam_intrinsics_cameras.emplace(7U, cache);
    CP2CompositeStateCapture capture;
    EXPECT_EQ(CP2CompositeStateAdapter::Capture(
                  malformed.state, CP2StatePhase::kPhase0Prior, capture),
              CP2CompositeStateStatus::kIncompleteStructure);
  }
}

TEST(CP2CompositePostcommit,
     PreparedPhase3FillIsNoexceptAllocationFreeAndMatchesProductionCommit) {
  static_assert(
      !std::is_copy_constructible<CP2PreparedPostcommitCapture>::value &&
          !std::is_copy_assignable<CP2PreparedPostcommitCapture>::value &&
          !std::is_move_constructible<CP2PreparedPostcommitCapture>::value &&
          !std::is_move_assignable<CP2PreparedPostcommitCapture>::value,
      "terminal phase-3 lifecycle objects must not be externally reset");
  static_assert(
      noexcept(CP2CompositeStateAdapter::FillPostcommitNoAlloc(
          std::declval<const std::shared_ptr<ov_msckf::State> &>(),
          std::declval<CP2PreparedPostcommitCapture &>())),
      "phase-3 fill must be statically noexcept");
  static_assert(
      noexcept(CP2CompositeStateAdapter::HandoffPostcommitNoAlloc(
          std::declval<CP2PreparedPostcommitCapture &>(),
          std::declval<std::unique_ptr<CP2CompositeStateSnapshot> &>())),
      "phase-3 owning handoff must be statically noexcept");

  // Prove that the injection used below is live for both the C heap and the
  // nonthrowing C++ allocation surface before relying on a zero-attempt count.
  const std::uint64_t probe_before =
      cp2_allocation_probe::new_calls.load(std::memory_order_relaxed);
  cp2_allocation_probe::fail_new.store(true, std::memory_order_relaxed);
  void *const malloc_failure = std::malloc(64U);
  void *const calloc_failure = std::calloc(2U, 32U);
  void *const realloc_failure = std::realloc(nullptr, 64U);
  void *const nothrow_new_failure = ::operator new(64U, std::nothrow);
  void *aligned_failure = reinterpret_cast<void *>(UINTPTR_MAX);
  const int posix_failure = posix_memalign(&aligned_failure, 32U, 64U);
  cp2_allocation_probe::fail_new.store(false, std::memory_order_relaxed);
  EXPECT_EQ(malloc_failure, nullptr);
  EXPECT_EQ(calloc_failure, nullptr);
  EXPECT_EQ(realloc_failure, nullptr);
  EXPECT_EQ(nothrow_new_failure, nullptr);
  EXPECT_EQ(posix_failure, ENOMEM);
  EXPECT_EQ(aligned_failure, nullptr);
  EXPECT_GE(cp2_allocation_probe::new_calls.load(std::memory_order_relaxed) -
                probe_before,
            UINT64_C(5));

  LiveCompositeFixture fixture = live_fixture(false);
  CP2CompositeStateCapture phase0;
  ASSERT_EQ(CP2CompositeStateAdapter::Capture(
                fixture.state, CP2StatePhase::kPhase0Prior, phase0),
            CP2CompositeStateStatus::kAccepted);
  CP2PreparedPostcommitCapture prepared;
  ASSERT_EQ(CP2CompositeStateAdapter::PreparePostcommit(phase0, prepared),
            CP2CompositeStateStatus::kAccepted);
  ASSERT_TRUE(prepared.prepared());
  EXPECT_EQ(prepared.lifecycle(),
            ov_msckf::CP2PreparedPostcommitState::kPrepared);

  const Eigen::Index dimension = phase0.snapshot.covariance.rows();
  const Eigen::MatrixXd H = Eigen::MatrixXd::Identity(dimension, dimension);
  Eigen::VectorXd residual(dimension);
  for (Eigen::Index index = 0; index < dimension; ++index) {
    residual(index) = 0.00025 * static_cast<double>(index + 1);
  }
  const Eigen::MatrixXd R = 0.5 * Eigen::MatrixXd::Identity(dimension, dimension);
  const ov_msckf::MSCKFUpdatePreviewResult preview =
      ov_msckf::UpdaterMSCKFPreview::Compute(fixture.state, fixture.top_level, H,
                                             residual, R);
  ASSERT_TRUE(preview.accepted());
  CP2CompositeStateSnapshot expected;
  std::uint64_t update_calls = 0U;
  ASSERT_EQ(CP2CompositeStateAdapter::BuildExpected(
                phase0.snapshot, preview, expected, update_calls),
            CP2CompositeStateStatus::kAccepted);
  ASSERT_EQ(update_calls, fixture.top_level.size());

  ov_msckf::StateHelper::EKFUpdate(fixture.state, fixture.top_level, H, residual, R);
  const std::uint64_t allocations_before =
      cp2_allocation_probe::new_calls.load(std::memory_order_relaxed);
  cp2_allocation_probe::fail_new.store(true, std::memory_order_relaxed);
  const CP2CompositeStateStatus fill_status =
      CP2CompositeStateAdapter::FillPostcommitNoAlloc(fixture.state, prepared);
  std::unique_ptr<CP2CompositeStateSnapshot> handed_off;
  const CP2CompositeStateStatus handoff_status =
      CP2CompositeStateAdapter::HandoffPostcommitNoAlloc(prepared, handed_off);
  cp2_allocation_probe::fail_new.store(false, std::memory_order_relaxed);
  const std::uint64_t allocations_after =
      cp2_allocation_probe::new_calls.load(std::memory_order_relaxed);
  EXPECT_EQ(fill_status, CP2CompositeStateStatus::kAccepted);
  EXPECT_EQ(handoff_status, CP2CompositeStateStatus::kAccepted);
  EXPECT_EQ(allocations_after, allocations_before);
  EXPECT_EQ(prepared.lifecycle(),
            ov_msckf::CP2PreparedPostcommitState::kHandedOff);
  EXPECT_FALSE(prepared.complete());
  ASSERT_NE(handed_off, nullptr);
  EXPECT_EQ(handed_off->phase, CP2StatePhase::kPhase3LivePostcommit);
  EXPECT_TRUE(CP2CompositeStateAdapter::CanonicallyEqual(expected, *handed_off));
  EXPECT_EQ(CP2CompositeStateAdapter::Validate(*handed_off),
            CP2CompositeStateStatus::kAccepted);
  EXPECT_EQ(CP2CompositeStateAdapter::FillPostcommitNoAlloc(fixture.state,
                                                             prepared),
            CP2CompositeStateStatus::kNotPrepared);
  std::unique_ptr<CP2CompositeStateSnapshot> second_handoff;
  EXPECT_EQ(CP2CompositeStateAdapter::HandoffPostcommitNoAlloc(
                prepared, second_handoff),
            CP2CompositeStateStatus::kNotPrepared);
}

TEST(CP2CompositePostcommit,
     LiveCacheReadAndPreparedOrPointerFailuresReturnExplicitStatus) {
  {
    LiveCompositeFixture fixture = live_fixture(false);
    CP2CompositeStateCapture phase0;
    ASSERT_EQ(CP2CompositeStateAdapter::Capture(
                  fixture.state, CP2StatePhase::kPhase0Prior, phase0),
              CP2CompositeStateStatus::kAccepted);
    CP2PreparedPostcommitCapture prepared;
    ASSERT_EQ(CP2CompositeStateAdapter::PreparePostcommit(phase0, prepared),
              CP2CompositeStateStatus::kAccepted);
    Eigen::MatrixXd changed_cache =
        fixture.state->_cam_intrinsics_cameras.at(0)->get_value();
    changed_cache(0, 0) += 3.0;
    fixture.state->_cam_intrinsics_cameras.at(0)->set_value(changed_cache);
    ASSERT_EQ(CP2CompositeStateAdapter::FillPostcommitNoAlloc(
                  fixture.state, prepared),
              CP2CompositeStateStatus::kAccepted);
    std::unique_ptr<CP2CompositeStateSnapshot> handed_off;
    ASSERT_EQ(CP2CompositeStateAdapter::HandoffPostcommitNoAlloc(
                  prepared, handed_off),
              CP2CompositeStateStatus::kAccepted);
    ASSERT_NE(handed_off, nullptr);
    EXPECT_TRUE(matrix_bits_equal(
        handed_off->camera_caches.at(0).calibration, changed_cache));
    EXPECT_FALSE(CP2CompositeStateAdapter::CanonicallyEqual(
        phase0.snapshot, *handed_off));
  }


  {
    LiveCompositeFixture fixture = live_fixture(false);
    CP2CompositeStateCapture phase0;
    ASSERT_EQ(CP2CompositeStateAdapter::Capture(
                  fixture.state, CP2StatePhase::kPhase0Prior, phase0),
              CP2CompositeStateStatus::kAccepted);
    CP2PreparedPostcommitCapture prepared;
    ASSERT_EQ(CP2CompositeStateAdapter::PreparePostcommit(phase0, prepared),
              CP2CompositeStateStatus::kAccepted);
    Eigen::MatrixXd nonfinite_imu = fixture.state->_imu->value();
    nonfinite_imu(4, 0) = binary64_from_bits(UINT64_C(0x7ff80000000000a5));
    fixture.state->_imu->set_value(nonfinite_imu);
    const std::uint64_t allocations_before =
        cp2_allocation_probe::new_calls.load(std::memory_order_relaxed);
    cp2_allocation_probe::fail_new.store(true, std::memory_order_relaxed);
    const CP2CompositeStateStatus status =
        CP2CompositeStateAdapter::FillPostcommitNoAlloc(fixture.state, prepared);
    std::unique_ptr<CP2CompositeStateSnapshot> handed_off;
    const CP2CompositeStateStatus handoff_status =
        CP2CompositeStateAdapter::HandoffPostcommitNoAlloc(prepared, handed_off);
    cp2_allocation_probe::fail_new.store(false, std::memory_order_relaxed);
    EXPECT_EQ(status, CP2CompositeStateStatus::kAccepted);
    EXPECT_EQ(handoff_status, CP2CompositeStateStatus::kAccepted);
    EXPECT_EQ(cp2_allocation_probe::new_calls.load(std::memory_order_relaxed),
              allocations_before);
    ASSERT_NE(handed_off, nullptr);
    EXPECT_EQ(CP2CompositeStateAdapter::Validate(*handed_off),
              CP2CompositeStateStatus::kNonfinite);
    EXPECT_EQ(binary64_bits(handed_off->active_types.at(0).nominal(4, 0)),
              UINT64_C(0x7ff80000000000a5));
  }

  {
    LiveCompositeFixture fixture = live_fixture(false);
    CP2CompositeStateCapture phase0;
    ASSERT_EQ(CP2CompositeStateAdapter::Capture(
                  fixture.state, CP2StatePhase::kPhase0Prior, phase0),
              CP2CompositeStateStatus::kAccepted);
    CP2PreparedPostcommitCapture prepared;
    ASSERT_EQ(CP2CompositeStateAdapter::PreparePostcommit(phase0, prepared),
              CP2CompositeStateStatus::kAccepted);
    const Eigen::MatrixXd calibration =
        fixture.state->_cam_intrinsics_cameras.at(0)->get_value();
    auto replacement = std::make_shared<ov_core::CamRadtan>(752, 480);
    replacement->set_value(calibration);
    fixture.state->_cam_intrinsics_cameras.at(0) = replacement;
    const std::uint64_t allocations_before =
        cp2_allocation_probe::new_calls.load(std::memory_order_relaxed);
    cp2_allocation_probe::fail_new.store(true, std::memory_order_relaxed);
    const CP2CompositeStateStatus status =
        CP2CompositeStateAdapter::FillPostcommitNoAlloc(fixture.state, prepared);
    cp2_allocation_probe::fail_new.store(false, std::memory_order_relaxed);
    EXPECT_EQ(status, CP2CompositeStateStatus::kInvalidPointerGraph);
    EXPECT_EQ(prepared.lifecycle(),
              ov_msckf::CP2PreparedPostcommitState::kFailed);
    EXPECT_EQ(cp2_allocation_probe::new_calls.load(std::memory_order_relaxed),
              allocations_before);
  }

  {
    LiveCompositeFixture fixture = live_fixture(false);
    CP2CompositeStateCapture phase0;
    ASSERT_EQ(CP2CompositeStateAdapter::Capture(
                  fixture.state, CP2StatePhase::kPhase0Prior, phase0),
              CP2CompositeStateStatus::kAccepted);
    CP2PreparedPostcommitCapture never_prepared;
    EXPECT_EQ(CP2CompositeStateAdapter::FillPostcommitNoAlloc(
                  fixture.state, never_prepared),
              CP2CompositeStateStatus::kNotPrepared);

    CP2PreparedPostcommitCapture null_attempt;
    ASSERT_EQ(CP2CompositeStateAdapter::PreparePostcommit(phase0, null_attempt),
              CP2CompositeStateStatus::kAccepted);
    const std::shared_ptr<ov_msckf::State> null_state;
    EXPECT_EQ(CP2CompositeStateAdapter::FillPostcommitNoAlloc(
                  null_state, null_attempt),
              CP2CompositeStateStatus::kNullState);
    EXPECT_EQ(null_attempt.lifecycle(),
              ov_msckf::CP2PreparedPostcommitState::kFailed);
    EXPECT_EQ(CP2CompositeStateAdapter::FillPostcommitNoAlloc(
                  fixture.state, null_attempt),
              CP2CompositeStateStatus::kNotPrepared);

    CP2CompositeStateCapture phase1;
    ASSERT_EQ(CP2CompositeStateAdapter::Capture(
                  fixture.state, CP2StatePhase::kPhase1Precommit, phase1),
              CP2CompositeStateStatus::kAccepted);
    EXPECT_TRUE(phase1.pointer_graph.empty());
    CP2PreparedPostcommitCapture phase1_substitution;
    EXPECT_EQ(CP2CompositeStateAdapter::PreparePostcommit(
                  phase1, phase1_substitution),
              CP2CompositeStateStatus::kInvalidPhase);
  }

  {
    LiveCompositeFixture authority = live_fixture(false);
    LiveCompositeFixture different_state = live_fixture(false);
    CP2CompositeStateCapture phase0;
    ASSERT_EQ(CP2CompositeStateAdapter::Capture(
                  authority.state, CP2StatePhase::kPhase0Prior, phase0),
              CP2CompositeStateStatus::kAccepted);
    different_state.state->_timestamp = std::nextafter(
        different_state.state->_timestamp,
        std::numeric_limits<double>::infinity());
    CP2CompositeStateCapture different_phase0;
    ASSERT_EQ(CP2CompositeStateAdapter::Capture(
                  different_state.state, CP2StatePhase::kPhase0Prior,
                  different_phase0),
              CP2CompositeStateStatus::kAccepted);
    CP2CompositeStateCapture mixed_authority = phase0;
    mixed_authority.pointer_graph = different_phase0.pointer_graph;
    CP2PreparedPostcommitCapture mixed_preparation;
    EXPECT_EQ(CP2CompositeStateAdapter::PreparePostcommit(
                  mixed_authority, mixed_preparation),
              CP2CompositeStateStatus::kInvalidPointerGraph);

    CP2PreparedPostcommitCapture prepared;
    ASSERT_EQ(CP2CompositeStateAdapter::PreparePostcommit(phase0, prepared),
              CP2CompositeStateStatus::kAccepted);
    CP2PreparedPostcommitCapture duplicate_preparation;
    EXPECT_EQ(CP2CompositeStateAdapter::PreparePostcommit(
                  phase0, duplicate_preparation),
              CP2CompositeStateStatus::kNotPrepared);
    EXPECT_EQ(duplicate_preparation.lifecycle(),
              ov_msckf::CP2PreparedPostcommitState::kEmpty);
    EXPECT_EQ(CP2CompositeStateAdapter::PreparePostcommit(phase0, prepared),
              CP2CompositeStateStatus::kNotPrepared);
    EXPECT_EQ(CP2CompositeStateAdapter::FillPostcommitNoAlloc(
                  different_state.state, prepared),
              CP2CompositeStateStatus::kInvalidPointerGraph);
    EXPECT_EQ(prepared.lifecycle(),
              ov_msckf::CP2PreparedPostcommitState::kFailed);
  }

  {
    LiveCompositeFixture fixture = live_fixture(false);
    CP2CompositeStateCapture phase0;
    ASSERT_EQ(CP2CompositeStateAdapter::Capture(
                  fixture.state, CP2StatePhase::kPhase0Prior, phase0),
              CP2CompositeStateStatus::kAccepted);
    CP2PreparedPostcommitCapture prepared;
    ASSERT_EQ(CP2CompositeStateAdapter::PreparePostcommit(phase0, prepared),
              CP2CompositeStateStatus::kAccepted);
    ASSERT_EQ(CP2CompositeStateAdapter::FillPostcommitNoAlloc(
                  fixture.state, prepared),
              CP2CompositeStateStatus::kAccepted);
    std::unique_ptr<CP2CompositeStateSnapshot> occupied(
        new CP2CompositeStateSnapshot());
    EXPECT_EQ(CP2CompositeStateAdapter::HandoffPostcommitNoAlloc(
                  prepared, occupied),
              CP2CompositeStateStatus::kInvalidProposal);
    EXPECT_EQ(prepared.lifecycle(),
              ov_msckf::CP2PreparedPostcommitState::kFailed);
    occupied.reset();
    EXPECT_EQ(CP2CompositeStateAdapter::HandoffPostcommitNoAlloc(
                  prepared, occupied),
              CP2CompositeStateStatus::kNotPrepared);
    EXPECT_EQ(occupied, nullptr);
  }
}

TEST(CP2CompositeDetachedOracle, AppliesExactlyOneProductionUpdatePerTopLevelType) {
  const CP2CompositeStateSnapshot prior =
      full_snapshot(CP2StatePhase::kPhase0Prior);
  const Eigen::Index dimension = prior.covariance.rows();
  Eigen::VectorXd dx(dimension);
  for (Eigen::Index index = 0; index < dimension; ++index) {
    dx(index) = 0.0005 * static_cast<double>(index + 1);
  }
  Eigen::MatrixXd P_plus = prior.covariance;
  P_plus.diagonal().array() += 0.03125;
  ov_msckf::MSCKFUpdatePreviewResult accepted_proposal;
  accepted_proposal.diagnostics.status =
      ov_msckf::MSCKFUpdatePreviewStatus::kAccepted;
  accepted_proposal.diagnostics.stage =
      ov_msckf::MSCKFUpdatePreviewStage::kAccepted;
  accepted_proposal.diagnostics.state_dimension = dimension;
  accepted_proposal.dx = dx;
  accepted_proposal.P_plus = P_plus;

  CP2CompositeStateSnapshot expected;
  std::uint64_t update_calls = UINT64_MAX;
  ASSERT_EQ(CP2CompositeStateAdapter::BuildExpected(
                prior, accepted_proposal, expected, update_calls),
            CP2CompositeStateStatus::kAccepted);
  EXPECT_EQ(update_calls, prior.active_types.size());
  EXPECT_EQ(expected.phase, CP2StatePhase::kPhase2ExpectedPostcommit);
  EXPECT_TRUE(matrix_bits_equal(expected.covariance, P_plus));
  ASSERT_EQ(expected.active_types.size(), prior.active_types.size());

  for (std::size_t index = 0; index < prior.active_types.size(); ++index) {
    const CP2ActiveStateType &source = prior.active_types[index];
    std::shared_ptr<ov_type::Type> detached;
    if (source.tag == CP2ActiveStateTypeTag::kImu) {
      detached = std::make_shared<ov_type::IMU>();
    } else if (source.tag == CP2ActiveStateTypeTag::kClone) {
      detached = std::make_shared<ov_type::PoseJPL>();
    } else {
      detached = std::make_shared<ov_type::Vec>(static_cast<int>(source.error_size));
    }
    detached->set_local_id(static_cast<int>(source.covariance_id));
    detached->set_value(source.nominal);
    detached->set_fej(source.fej);
    detached->update(dx.segment(static_cast<Eigen::Index>(source.covariance_id),
                                static_cast<Eigen::Index>(source.error_size)));
    EXPECT_TRUE(matrix_bits_equal(expected.active_types[index].nominal,
                                  detached->value()));
    EXPECT_TRUE(matrix_bits_equal(expected.active_types[index].fej, source.fej));
    EXPECT_EQ(expected.active_types[index].tag, source.tag);
    EXPECT_EQ(expected.active_types[index].landmark.feature_id,
              source.landmark.feature_id);
    EXPECT_EQ(binary64_bits(expected.active_types[index].landmark.anchor_timestamp),
              binary64_bits(source.landmark.anchor_timestamp));
  }
  EXPECT_TRUE(CP2CompositeStateAdapter::CanonicallyEqual(
      prior, full_snapshot(CP2StatePhase::kPhase0Prior)));
  EXPECT_EQ(CP2CompositeStateAdapter::Validate(expected),
            CP2CompositeStateStatus::kAccepted);

  CP2CompositeStateSnapshot rejected;
  update_calls = 999U;
  ov_msckf::MSCKFUpdatePreviewResult malformed_proposal = accepted_proposal;
  malformed_proposal.dx = dx.head(dx.rows() - 1);
  EXPECT_EQ(CP2CompositeStateAdapter::BuildExpected(
                prior, malformed_proposal, rejected, update_calls),
            CP2CompositeStateStatus::kInvalidProposal);
  EXPECT_EQ(update_calls, 0U);
  update_calls = 999U;
  malformed_proposal = accepted_proposal;
  malformed_proposal.P_plus =
      P_plus.topLeftCorner(dimension - 1, dimension - 1);
  EXPECT_EQ(CP2CompositeStateAdapter::BuildExpected(
                prior, malformed_proposal, rejected, update_calls),
            CP2CompositeStateStatus::kInvalidProposal);
  EXPECT_EQ(update_calls, 0U);
  update_calls = 999U;
  malformed_proposal = accepted_proposal;
  malformed_proposal.dx(0) = std::numeric_limits<double>::infinity();
  EXPECT_EQ(CP2CompositeStateAdapter::BuildExpected(
                prior, malformed_proposal, rejected, update_calls),
            CP2CompositeStateStatus::kInvalidProposal);
  EXPECT_EQ(update_calls, 0U);
  update_calls = 999U;
  malformed_proposal = accepted_proposal;
  malformed_proposal.diagnostics.status =
      ov_msckf::MSCKFUpdatePreviewStatus::kInvalidInput;
  EXPECT_EQ(CP2CompositeStateAdapter::BuildExpected(
                prior, malformed_proposal, rejected, update_calls),
            CP2CompositeStateStatus::kInvalidProposal);
  EXPECT_EQ(update_calls, 0U);
  update_calls = 999U;
  malformed_proposal = accepted_proposal;
  malformed_proposal.diagnostics.repair_count = 1U;
  EXPECT_EQ(CP2CompositeStateAdapter::BuildExpected(
                prior, malformed_proposal, rejected, update_calls),
            CP2CompositeStateStatus::kInvalidProposal);
  EXPECT_EQ(update_calls, 0U);
}

} // namespace
