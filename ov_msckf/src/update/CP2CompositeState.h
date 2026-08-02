/*
 * SchurVIO-Lite CP2 owning composite-state boundary and commit oracle.
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#ifndef OV_MSCKF_CP2_COMPOSITE_STATE_H
#define OV_MSCKF_CP2_COMPOSITE_STATE_H

#include "UpdaterMSCKFPreview.h"

#include <Eigen/Core>

#include <cstdint>
#include <memory>
#include <vector>

namespace ov_msckf {

class State;
struct CP2CompositePointerGraphData;

/// Frozen state-file phase values. The enum itself is not part of a payload.
enum class CP2StatePhase : std::uint8_t {
  kPhase0Prior = 0,
  kPhase1Precommit = 1,
  kPhase2ExpectedPostcommit = 2,
  kPhase3LivePostcommit = 3,
};

enum class CP2SemanticStateKind : std::uint8_t {
  kImuTheta,
  kImuPosition,
  kImuVelocity,
  kImuGyroBias,
  kImuAccelBias,
  kCloneTheta,
  kClonePosition,
  kSlamLandmark,
};

enum class CP2ActiveStateTypeTag : std::uint8_t {
  kImu,
  kClone,
  kSlamLandmark,
};

enum class CP2FixedImuCalibrationTag : std::uint8_t {
  kDw,
  kDa,
  kTg,
  kGyroToImu,
  kAccelToImu,
};

enum class CP2LandmarkRepresentation : std::uint8_t {
  kGlobal3D,
  kGlobalFullInverseDepth,
  kAnchored3D,
  kAnchoredFullInverseDepth,
  kAnchoredMsckfInverseDepth,
  kAnchoredInverseDepthSingle,
  kUnknown,
};

/// Ordered result of composite capture/validation/oracle operations.
enum class CP2CompositeStateStatus : std::uint8_t {
  kAccepted,
  kNullState,
  kInvalidPhase,
  kIncompleteStructure,
  kInvalidPointerGraph,
  kInvalidActivePartition,
  kInvalidSemanticPartition,
  kInvalidIdentity,
  kInvalidShape,
  kNonfinite,
  kInvalidQuaternion,
  kInvalidProposal,
  kNotPrepared,
};

/// One-way lifecycle of storage reserved before the live baseline commit.
enum class CP2PreparedPostcommitState : std::uint8_t {
  kEmpty,
  kPrepared,
  kComplete,
  kFailed,
  kHandedOff,
};

const char *cp2_composite_state_status_name(CP2CompositeStateStatus status) noexcept;

bool cp2_state_phase_is_prior(CP2StatePhase phase) noexcept;
bool cp2_state_phase_is_postcommit(CP2StatePhase phase) noexcept;

struct CP2LandmarkIdentity {
  std::uint64_t feature_id = 0;
  CP2LandmarkRepresentation representation = CP2LandmarkRepresentation::kUnknown;
  std::int64_t anchor_camera_id = -1;
  double anchor_timestamp = -1.0;
};

struct CP2SemanticStateBlock {
  CP2SemanticStateKind kind = CP2SemanticStateKind::kImuTheta;
  std::uint64_t covariance_id = 0;
  std::uint64_t error_size = 0;
  double clone_timestamp = 0.0;
  CP2LandmarkIdentity landmark;
};

struct CP2ActiveStateType {
  CP2ActiveStateTypeTag tag = CP2ActiveStateTypeTag::kImu;
  std::uint64_t covariance_id = 0;
  std::uint64_t error_size = 0;
  double clone_timestamp = 0.0;
  CP2LandmarkIdentity landmark;
  Eigen::MatrixXd nominal;
  Eigen::MatrixXd fej;
};

struct CP2FixedImuCalibration {
  CP2FixedImuCalibrationTag tag = CP2FixedImuCalibrationTag::kDw;
  Eigen::MatrixXd nominal;
  Eigen::MatrixXd fej;
};

struct CP2CameraFixedCalibration {
  std::uint64_t camera_id = 0;
  Eigen::MatrixXd extrinsic_nominal;
  Eigen::MatrixXd extrinsic_fej;
  Eigen::MatrixXd intrinsic_nominal;
  Eigen::MatrixXd intrinsic_fej;
};

struct CP2CameraModelCache {
  std::uint64_t camera_id = 0;
  std::int64_t width = 0;
  std::int64_t height = 0;
  Eigen::MatrixXd calibration;
  Eigen::MatrixXd K;
  Eigen::MatrixXd D;
};

/**
 * Complete owning value layout shared by all four phases.
 *
 * The phase chooses only the canonical domain. It is deliberately excluded
 * from the composite field bytes, so phases 0/1 and phases 2/3 can be compared
 * byte-for-byte. Pointer identity is held separately and is never serialized.
 */
struct CP2CompositeStateSnapshot {
  CP2StatePhase phase = CP2StatePhase::kPhase0Prior;
  double timestamp = 0.0;
  Eigen::MatrixXd covariance;
  std::vector<CP2SemanticStateBlock> semantic_blocks;
  std::vector<CP2ActiveStateType> active_types;
  std::vector<CP2FixedImuCalibration> fixed_imu_calibrations;
  Eigen::MatrixXd time_offset_nominal;
  Eigen::MatrixXd time_offset_fej;
  std::vector<CP2CameraFixedCalibration> camera_calibrations;
  std::vector<CP2CameraModelCache> camera_caches;
};

/**
 * Opaque owning same-process identity proof. Raw addresses are never exposed,
 * serialized, logged, or hashed.
 */
class CP2CompositePointerGraphToken {
public:
  CP2CompositePointerGraphToken() = default;
  bool empty() const noexcept { return !data_; }

private:
  std::shared_ptr<const CP2CompositePointerGraphData> data_;
  friend class CP2CompositeStateAdapter;
};

struct CP2CompositeStateCapture {
  CP2CompositeStateSnapshot snapshot;
  CP2CompositePointerGraphToken pointer_graph;
};

/**
 * Pre-sized value-owning phase-3 storage allocated before commit.
 *
 * The snapshot itself is heap-owned so the completed value can be transferred
 * to the authoritative sink with a single noexcept unique_ptr move. The
 * lifecycle is deliberately one-way: a completed or failed capture cannot be
 * filled again.
 */
class CP2PreparedPostcommitCapture {
public:
  CP2PreparedPostcommitCapture() = default;
  ~CP2PreparedPostcommitCapture() = default;
  CP2PreparedPostcommitCapture(const CP2PreparedPostcommitCapture &) = delete;
  CP2PreparedPostcommitCapture &
  operator=(const CP2PreparedPostcommitCapture &) = delete;
  CP2PreparedPostcommitCapture(CP2PreparedPostcommitCapture &&) = delete;
  CP2PreparedPostcommitCapture &
  operator=(CP2PreparedPostcommitCapture &&) = delete;

  bool prepared() const noexcept {
    return state_ == CP2PreparedPostcommitState::kPrepared && snapshot_ != nullptr;
  }
  bool complete() const noexcept {
    return state_ == CP2PreparedPostcommitState::kComplete && snapshot_ != nullptr;
  }
  CP2PreparedPostcommitState lifecycle() const noexcept { return state_; }

private:
  std::unique_ptr<CP2CompositeStateSnapshot> snapshot_;
  CP2PreparedPostcommitState state_ = CP2PreparedPostcommitState::kEmpty;
  CP2CompositePointerGraphToken pointer_graph_;
  friend class CP2CompositeStateAdapter;
};

class CP2CompositeStateAdapter {
public:
  /**
   * Capture every schema field and an owning pointer graph from live State.
   *
   * Capture is lossless and does not reject nonfinite values or invalid unit
   * quaternions; the caller must invoke Validate separately. A complete phase
   * 1 is compared bitwise and structurally with phase 0 before any independent
   * phase-1 validity classification. This distinction is required so a changed
   * phase 1 or complete failed phase 3 remains representable with its exact
   * binary64 bits.
   */
  static CP2CompositeStateStatus Capture(const std::shared_ptr<State> &state,
                                         CP2StatePhase phase,
                                         CP2CompositeStateCapture &output);

  /// Passing-record validation, including all finite/quaternion requirements.
  static CP2CompositeStateStatus
  Validate(const CP2CompositeStateSnapshot &snapshot) noexcept;

  /// Exact structural and binary64 field equality; compatible phase is ignored.
  static bool CanonicallyEqual(const CP2CompositeStateSnapshot &left,
                               const CP2CompositeStateSnapshot &right) noexcept;

  /// Allocation-free comparison of the live graph with the owning phase-0 token.
  static bool PointerGraphMatches(const std::shared_ptr<State> &state,
                                  const CP2CompositePointerGraphToken &token) noexcept;

  /// Sole value-only projection used by every gate and preview after promotion.
  static CP2CompositeStateStatus
  ProjectPreview(const CP2CompositeStateSnapshot &snapshot,
                 MSCKFUpdatePreviewSnapshot &output);

  /**
   * Build phase 2 by applying one complete production Type::update per active
   * top-level object. No live object is read or written.
   */
  static CP2CompositeStateStatus
  BuildExpected(const CP2CompositeStateSnapshot &phase0,
                const MSCKFUpdatePreviewResult &accepted_baseline_proposal,
                CP2CompositeStateSnapshot &output,
                std::uint64_t &type_update_calls);

  /// Allocate phase-3 storage and bind it to this exact phase-0 value/token pair.
  static CP2CompositeStateStatus
  PreparePostcommit(const CP2CompositeStateCapture &phase0,
                    CP2PreparedPostcommitCapture &output);

  /**
   * Fill the prepared phase-3 snapshot directly from live State.
   *
   * This boundary performs no allocation, is nonthrowing, and returns a
   * failure status rather than publishing an incomplete snapshot.
   */
  static CP2CompositeStateStatus
  FillPostcommitNoAlloc(const std::shared_ptr<State> &state,
                        CP2PreparedPostcommitCapture &prepared) noexcept;

  /**
   * Transfer the complete owning phase-3 value into an empty sink slot.
   *
   * This is the allocation-free/nonthrowing handoff primitive used by the
   * later authoritative recorded sink. It consumes the prepared object once.
   */
  static CP2CompositeStateStatus HandoffPostcommitNoAlloc(
      CP2PreparedPostcommitCapture &prepared,
      std::unique_ptr<CP2CompositeStateSnapshot> &output) noexcept;

#if defined(OV_MSCKF_CP2_TESTING)
  /// Closed, non-production controls for updater failure-wiring tests.
  static void TestInvalidatePreparedStorage(
      CP2PreparedPostcommitCapture &prepared) noexcept;
  static void TestInvalidatePreparedPointerToken(
      CP2PreparedPostcommitCapture &prepared) noexcept;
#endif
};

} // namespace ov_msckf

#endif // OV_MSCKF_CP2_COMPOSITE_STATE_H
