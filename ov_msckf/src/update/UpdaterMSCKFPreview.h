/*
 * OpenVINS: An Open Platform for Visual-Inertial Research
 * Copyright (C) 2018-2023 Patrick Geneva
 * Copyright (C) 2018-2023 Guoquan Huang
 * Copyright (C) 2018-2023 OpenVINS Contributors
 * Copyright (C) 2018-2019 Kevin Eckenhoff
 * Copyright (C) 2026 Moksh Trehan
 * Modified in 2026 by Moksh Trehan for SchurVIO-Lite CP2.
 *
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation, either version 3 of the License, or
 * (at your option) any later version.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.
 *
 * You should have received a copy of the GNU General Public License
 * along with this program.  If not, see <https://www.gnu.org/licenses/>.
 */

#ifndef OV_MSCKF_UPDATER_MSCKF_PREVIEW_H
#define OV_MSCKF_UPDATER_MSCKF_PREVIEW_H

#include <Eigen/Core>

#include <cstddef>
#include <cstdint>
#include <limits>
#include <memory>
#include <vector>

namespace ov_type {
class Type;
} // namespace ov_type

namespace ov_msckf {

class State;

/// Terminal outcome of the read-only MSCKF EKF update preflight.
enum class MSCKFUpdatePreviewStatus {
  kAccepted,
  kInvalidInput,
  kNonfinite,
  kFactorizationFailed,
  kNegativeDiagonal,
};

/// Exact terminal stage, retained for deterministic rejection diagnostics.
enum class MSCKFUpdatePreviewStage {
  kState,
  kInputDimensions,
  kStateOrder,
  kRawInputs,
  kCrossCovariance,
  kMarginalCovariance,
  kInnovation,
  kInnovationFactorization,
  kInnovationInverse,
  kKalmanGain,
  kPosteriorCovariance,
  kPosteriorDiagonal,
  kStateIncrement,
  kAccepted,
};

/// Stable lower-case names for machine-readable diagnostics.
const char *msckf_update_preview_status_name(MSCKFUpdatePreviewStatus status) noexcept;
const char *msckf_update_preview_stage_name(MSCKFUpdatePreviewStage stage) noexcept;

/**
 * @brief Read-only diagnostics for one compressed MSCKF update proposal.
 *
 * The zero-valued counters make the CP2 no-repair policy explicit in
 * traces. This helper contains no jitter, repair, regularization, or alternate
 * solve path.
 */
struct MSCKFUpdatePreviewDiagnostics {
  MSCKFUpdatePreviewStatus status = MSCKFUpdatePreviewStatus::kInvalidInput;
  MSCKFUpdatePreviewStage stage = MSCKFUpdatePreviewStage::kState;

  Eigen::Index state_dimension = 0;
  Eigen::Index measurement_dimension = 0;
  Eigen::Index jacobian_dimension = 0;
  Eigen::Index ordered_jacobian_dimension = 0;

  Eigen::Index offending_order_index = -1;
  Eigen::Index offending_diagonal_index = -1;
  bool minimum_posterior_diagonal_available = false;
  double minimum_posterior_diagonal = std::numeric_limits<double>::quiet_NaN();

  std::size_t jitter_count = 0;
  std::size_t repair_count = 0;
  std::size_t alternate_solve_count = 0;
  std::size_t clamp_count = 0;
  std::size_t regularization_count = 0;
  std::size_t fallback_count = 0;
};

/**
 * @brief Full proposed error-state increment and posterior covariance.
 *
 * The proposal fields are populated only for an accepted result. Rejected
 * results therefore cannot accidentally be committed by a caller that ignores
 * the status.
 */
struct MSCKFUpdatePreviewResult {
  MSCKFUpdatePreviewDiagnostics diagnostics;
  Eigen::VectorXd dx;
  Eigen::MatrixXd P_plus;

  bool accepted() const noexcept { return diagnostics.status == MSCKFUpdatePreviewStatus::kAccepted; }
};

/**
 * @brief Read-only same-prior mean proposal without a posterior covariance.
 *
 * This is the fixed-two-pass numerical seam. It follows the same layout,
 * innovation, inverse, Kalman-gain, and increment operation order as
 * ComputeFromSnapshot(), but deliberately stops before forming P-plus. The
 * innovation statistic is evaluated from the same accepted innovation
 * inverse. No input or live estimator object is mutated.
 */
struct MSCKFUpdateMeanResult {
  MSCKFUpdatePreviewDiagnostics diagnostics;
  Eigen::VectorXd dx;
  double nis = std::numeric_limits<double>::quiet_NaN();

  bool accepted() const noexcept {
    return diagnostics.status == MSCKFUpdatePreviewStatus::kAccepted;
  }
};

/**
 * @brief One value-only block in an MSCKF update snapshot or Jacobian layout.
 *
 * For a snapshot state block, @p covariance_id and @p offset are both the
 * first row/column occupied by the block in the full covariance and must be
 * equal. For a Jacobian-layout block, @p
 * covariance_id is the first row/column occupied by the measured variable in
 * the full covariance and @p offset is its first column in the supplied
 * Jacobian. In both uses, @p size is the error-state dimension.
 *
 * Snapshot state blocks form an ordered, unique, complete partition of the
 * covariance with no identity/offset indirection. Jacobian blocks are ordered
 * by their explicit offsets, unique and nonoverlapping in covariance
 * coordinates, and cover every Jacobian column exactly once.
 */
struct MSCKFUpdatePreviewBlock {
  Eigen::Index covariance_id = -1;
  Eigen::Index size = 0;
  Eigen::Index offset = -1;
};

/**
 * @brief Owning value-only state input for an MSCKF update preview.
 *
 * This type intentionally contains no State, Type, Feature, or other live
 * object pointer. It is therefore suitable for the CP2 shadow and offline
 * replay paths, which must operate only on an immutable pre-mutation prior.
 */
struct MSCKFUpdatePreviewSnapshot {
  Eigen::MatrixXd covariance;
  std::vector<MSCKFUpdatePreviewBlock> state_blocks;
};

/// Owning current/FEJ values for one active error-state block.
struct MSCKFUpdatePriorNominalBlock {
  Eigen::Index covariance_id = -1;
  Eigen::Index size = 0;
  Eigen::MatrixXd value;
  Eigen::MatrixXd fej;
};

/// Stable semantic classification for one complete top-level covariance block.
enum class MSCKFUpdatePriorBlockType : std::uint64_t {
  kUnknown = 0U,
  kImu = 1U,
  kImuGyroscopeIntrinsics = 2U,
  kImuAccelerometerIntrinsics = 3U,
  kImuGravitySensitivity = 4U,
  kGyroscopeToImuRotation = 5U,
  kAccelerometerToImuRotation = 6U,
  kCameraTimeOffset = 7U,
  kCameraExtrinsics = 8U,
  kCameraIntrinsics = 9U,
  kClone = 10U,
  kSlamLandmark = 11U,
};

/// Stable key domain used to interpret a semantic covariance block.
enum class MSCKFUpdatePriorBlockKey : std::uint64_t {
  kNone = 0U,
  kTimestamp = 1U,
  kCameraId = 2U,
  kFeatureId = 3U,
};

/**
 * Complete semantic identity for one top-level active-state block.
 *
 * variable_id is the inherited Type::id() and therefore equals covariance_id
 * for a top-level block. role_flags bit 0 denotes a current nominal value and
 * bit 1 denotes a FEJ value; both are present in CapturePrior snapshots.
 */
struct MSCKFUpdatePriorSemanticBlock {
  std::uint64_t ordinal = 0U;
  MSCKFUpdatePriorBlockType block_type =
      MSCKFUpdatePriorBlockType::kUnknown;
  MSCKFUpdatePriorBlockKey key_type = MSCKFUpdatePriorBlockKey::kNone;
  std::uint64_t key_u64 = 0U;
  double key_double = 0.0;
  Eigen::Index variable_id = -1;
  Eigen::Index covariance_id = -1;
  Eigen::Index offset = -1;
  Eigen::Index size = 0;
  std::uint64_t role_flags = 0U;
};

/// Owning clone-to-covariance binding needed by visual linearization.
struct MSCKFUpdatePriorCloneBinding {
  double timestamp = 0.0;
  Eigen::Index covariance_id = -1;
};

enum class MSCKFUpdatePriorCameraModel {
  kUnknown,
  kRadtan,
  kEquidistant,
};

/// Owning camera calibration and projection-cache values at updater entry.
struct MSCKFUpdatePriorCamera {
  std::size_t camera_id = 0;
  Eigen::Index extrinsic_id = -1;
  Eigen::Index intrinsic_id = -1;
  Eigen::MatrixXd extrinsic_value;
  Eigen::MatrixXd extrinsic_fej;
  Eigen::MatrixXd intrinsic_value;
  Eigen::MatrixXd intrinsic_fej;
  Eigen::MatrixXd cache_value;
  int width = 0;
  int height = 0;
  MSCKFUpdatePriorCameraModel model =
      MSCKFUpdatePriorCameraModel::kUnknown;
};

/**
 * Owning predicted-prior boundary for one ordinary MSCKF invocation.
 *
 * In addition to the covariance/layout consumed by the numerical preview, it
 * freezes every active nominal and FEJ value plus the fixed camera quantities
 * read by visual linearization. It contains no live State or Type pointer.
 */
struct MSCKFUpdatePriorSnapshot {
  MSCKFUpdatePreviewSnapshot filter;
  double timestamp = 0.0;
  std::vector<MSCKFUpdatePriorNominalBlock> nominal_blocks;
  std::vector<MSCKFUpdatePriorSemanticBlock> semantic_blocks;
  std::vector<MSCKFUpdatePriorCloneBinding> clone_bindings;
  std::vector<MSCKFUpdatePriorCamera> cameras;
  bool do_fej = false;
  bool calibrate_camera_pose = false;
  bool calibrate_camera_intrinsics = false;
  bool calibrate_camera_timeoffset = false;
  bool calibrate_imu_intrinsics = false;
  bool calibrate_imu_g_sensitivity = false;
  int feature_representation = -1;
};

/// Exact reason an entry prior no longer matches the serialized live state.
enum class MSCKFUpdatePriorMatchStatus {
  kAccepted,
  kInvalidState,
  kCovarianceLayout,
  kCloneLayout,
  kCameraLayout,
  kOptions,
  kTimestamp,
  kCovariance,
  kNominal,
  kFej,
  kCameraValue,
};

struct MSCKFUpdatePriorMatchResult {
  MSCKFUpdatePriorMatchStatus status =
      MSCKFUpdatePriorMatchStatus::kInvalidState;
  Eigen::Index offending_index = -1;

  bool accepted() const noexcept {
    return status == MSCKFUpdatePriorMatchStatus::kAccepted;
  }
};

const char *msckf_update_prior_match_status_name(
    MSCKFUpdatePriorMatchStatus status) noexcept;

/**
 * @brief Read-only preview of StateHelper::EKFUpdate for an MSCKF system.
 *
 * The helper snapshots the full covariance, constructs the full-state
 * Jacobian from H_order, and follows the committed covariance-form operation:
 * upper-triangle innovation, LLT solve, Kalman gain, upper-triangle
 * subtractive covariance update, mirroring, diagonal validation, and state
 * increment. It never updates a Type, covariance, calibration object, or any
 * other live state field.
 *
 * The caller must provide the same external state serialization used by the
 * live updater; this function does not acquire State::_mutex_state.
 */
class UpdaterMSCKFPreview {
public:
  /**
   * @brief Capture the exact owning full-prior value boundary from live State.
   *
   * This friend-owned adapter copies State::_Cov and the exact State::_variables
   * top-level order. It performs no arithmetic and retains no live pointer.
   * External serialization of State access is required, as for Compute().
   */
  static MSCKFUpdatePreviewSnapshot CaptureSnapshot(const std::shared_ptr<State> &state);

  /// Capture covariance/layout, nominal, FEJ, clone, and camera values once.
  static MSCKFUpdatePriorSnapshot CapturePrior(
      const std::shared_ptr<State> &state);

  /// Exact, read-only validation that the live prior is still the entry prior.
  static MSCKFUpdatePriorMatchResult MatchPrior(
      const std::shared_ptr<State> &state,
      const MSCKFUpdatePriorSnapshot &snapshot);

  /**
   * @brief Compute a read-only proposal from owning value inputs only.
   *
   * The arithmetic and validation order after layout validation match
   * StateHelper::EKFUpdate exactly: block-ordered cross covariance, marginal
   * covariance, upper-triangle innovation, upper-triangle LLT inverse,
   * Kalman gain, subtractive/mirrored posterior, diagonal check, and state
   * increment. No input is mutated and no repair or fallback is attempted.
   *
   * @param snapshot Full covariance and ordered active-state partition.
   * @param jacobian_layout Value-only mapping from H columns to covariance.
   * @param H Globally compressed measurement Jacobian.
   * @param residual Globally compressed residual.
   * @param R Measurement covariance.
   * @return Accepted full proposal or an exact rejection stage.
   */
  static MSCKFUpdatePreviewResult
  ComputeFromSnapshot(const MSCKFUpdatePreviewSnapshot &snapshot,
                      const std::vector<MSCKFUpdatePreviewBlock> &jacobian_layout,
                      const Eigen::MatrixXd &H, const Eigen::VectorXd &residual,
                      const Eigen::MatrixXd &R);

  /**
   * @brief Compute only the absolute mean proposal and NIS from one frozen
   * prior.
   *
   * Unlike ComputeFromSnapshot(), this method never allocates or evaluates a
   * posterior covariance. It is therefore safe to call for both visual passes
   * before the selected pass is known.
   */
  static MSCKFUpdateMeanResult
  ComputeMeanFromSnapshot(
      const MSCKFUpdatePreviewSnapshot &snapshot,
      const std::vector<MSCKFUpdatePreviewBlock> &jacobian_layout,
      const Eigen::MatrixXd &H, const Eigen::VectorXd &residual,
      const Eigen::MatrixXd &R);

  /**
   * @param state State whose covariance is read without mutation.
   * @param H_order State-variable ordering represented by the columns of H.
   * @param H Globally compressed measurement Jacobian.
   * @param residual Globally compressed residual.
   * @param R Measurement covariance.
   * @return Accepted full proposal or an exact rejection stage.
   */
  static MSCKFUpdatePreviewResult Compute(const std::shared_ptr<State> &state,
                                          const std::vector<std::shared_ptr<ov_type::Type>> &H_order,
                                          const Eigen::MatrixXd &H, const Eigen::VectorXd &residual,
                                          const Eigen::MatrixXd &R);
};

} // namespace ov_msckf

#endif // OV_MSCKF_UPDATER_MSCKF_PREVIEW_H
