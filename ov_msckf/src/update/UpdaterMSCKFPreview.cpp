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

#include "UpdaterMSCKFPreview.h"

#include "cam/CamEqui.h"
#include "cam/CamRadtan.h"
#include "state/State.h"
#include "types/IMU.h"
#include "types/PoseJPL.h"
#include "types/Type.h"

#include <Eigen/Cholesky>

#include <algorithm>
#include <cstddef>
#include <cstring>
#include <utility>
#include <vector>

namespace ov_msckf {

namespace {

template <typename LeftDerived, typename RightDerived>
bool matrix_binary_equal(const Eigen::MatrixBase<LeftDerived> &left,
                         const Eigen::MatrixBase<RightDerived> &right) noexcept {
  if (left.rows() != right.rows() || left.cols() != right.cols()) {
    return false;
  }
  return left.size() == 0 ||
         std::memcmp(left.derived().data(), right.derived().data(),
                     static_cast<std::size_t>(left.size()) * sizeof(double)) ==
             0;
}

bool pose_storage_consistent(const ov_type::PoseJPL &pose,
                             bool fej) noexcept {
  Eigen::Matrix<double, 7, 1> effective;
  if (fej) {
    effective.block(0, 0, 4, 1) = pose.quat_fej();
    effective.block(4, 0, 3, 1) = pose.pos_fej();
    return matrix_binary_equal(pose.fej(), effective);
  }
  effective.block(0, 0, 4, 1) = pose.quat();
  effective.block(4, 0, 3, 1) = pose.pos();
  return matrix_binary_equal(pose.value(), effective);
}

bool imu_storage_consistent(const ov_type::IMU &imu, bool fej) noexcept {
  Eigen::Matrix<double, 16, 1> effective;
  if (fej) {
    effective.block(0, 0, 4, 1) = imu.quat_fej();
    effective.block(4, 0, 3, 1) = imu.pos_fej();
    effective.block(7, 0, 3, 1) = imu.vel_fej();
    effective.block(10, 0, 3, 1) = imu.bias_g_fej();
    effective.block(13, 0, 3, 1) = imu.bias_a_fej();
    return matrix_binary_equal(imu.fej(), effective);
  }
  effective.block(0, 0, 4, 1) = imu.quat();
  effective.block(4, 0, 3, 1) = imu.pos();
  effective.block(7, 0, 3, 1) = imu.vel();
  effective.block(10, 0, 3, 1) = imu.bias_g();
  effective.block(13, 0, 3, 1) = imu.bias_a();
  return matrix_binary_equal(imu.value(), effective);
}

bool type_storage_consistent(const std::shared_ptr<ov_type::Type> &variable,
                             bool fej) noexcept {
  if (const auto *imu = dynamic_cast<const ov_type::IMU *>(variable.get())) {
    return imu_storage_consistent(*imu, fej);
  }
  if (const auto *pose =
          dynamic_cast<const ov_type::PoseJPL *>(variable.get())) {
    return pose_storage_consistent(*pose, fej);
  }
  return true;
}

MSCKFUpdatePriorCameraModel
camera_model(const std::shared_ptr<ov_core::CamBase> &camera) noexcept {
  if (dynamic_cast<ov_core::CamRadtan *>(camera.get()) != nullptr) {
    return MSCKFUpdatePriorCameraModel::kRadtan;
  }
  if (dynamic_cast<ov_core::CamEqui *>(camera.get()) != nullptr) {
    return MSCKFUpdatePriorCameraModel::kEquidistant;
  }
  return MSCKFUpdatePriorCameraModel::kUnknown;
}

} // namespace

const char *msckf_update_preview_status_name(MSCKFUpdatePreviewStatus status) noexcept {
  switch (status) {
  case MSCKFUpdatePreviewStatus::kAccepted:
    return "accepted";
  case MSCKFUpdatePreviewStatus::kInvalidInput:
    return "invalid_input";
  case MSCKFUpdatePreviewStatus::kNonfinite:
    return "nonfinite";
  case MSCKFUpdatePreviewStatus::kFactorizationFailed:
    return "factorization_failed";
  case MSCKFUpdatePreviewStatus::kNegativeDiagonal:
    return "negative_diagonal";
  }
  return "unknown";
}

const char *msckf_update_preview_stage_name(MSCKFUpdatePreviewStage stage) noexcept {
  switch (stage) {
  case MSCKFUpdatePreviewStage::kState:
    return "state";
  case MSCKFUpdatePreviewStage::kInputDimensions:
    return "input_dimensions";
  case MSCKFUpdatePreviewStage::kStateOrder:
    return "state_order";
  case MSCKFUpdatePreviewStage::kRawInputs:
    return "raw_inputs";
  case MSCKFUpdatePreviewStage::kCrossCovariance:
    return "cross_covariance";
  case MSCKFUpdatePreviewStage::kMarginalCovariance:
    return "marginal_covariance";
  case MSCKFUpdatePreviewStage::kInnovation:
    return "innovation";
  case MSCKFUpdatePreviewStage::kInnovationFactorization:
    return "innovation_factorization";
  case MSCKFUpdatePreviewStage::kInnovationInverse:
    return "innovation_inverse";
  case MSCKFUpdatePreviewStage::kKalmanGain:
    return "kalman_gain";
  case MSCKFUpdatePreviewStage::kPosteriorCovariance:
    return "posterior_covariance";
  case MSCKFUpdatePreviewStage::kPosteriorDiagonal:
    return "posterior_diagonal";
  case MSCKFUpdatePreviewStage::kStateIncrement:
    return "state_increment";
  case MSCKFUpdatePreviewStage::kAccepted:
    return "accepted";
  }
  return "unknown";
}

const char *msckf_update_prior_match_status_name(
    MSCKFUpdatePriorMatchStatus status) noexcept {
  switch (status) {
  case MSCKFUpdatePriorMatchStatus::kAccepted:
    return "accepted";
  case MSCKFUpdatePriorMatchStatus::kInvalidState:
    return "invalid_state";
  case MSCKFUpdatePriorMatchStatus::kCovarianceLayout:
    return "covariance_layout";
  case MSCKFUpdatePriorMatchStatus::kCloneLayout:
    return "clone_layout";
  case MSCKFUpdatePriorMatchStatus::kCameraLayout:
    return "camera_layout";
  case MSCKFUpdatePriorMatchStatus::kOptions:
    return "options";
  case MSCKFUpdatePriorMatchStatus::kTimestamp:
    return "timestamp";
  case MSCKFUpdatePriorMatchStatus::kCovariance:
    return "covariance";
  case MSCKFUpdatePriorMatchStatus::kNominal:
    return "nominal";
  case MSCKFUpdatePriorMatchStatus::kFej:
    return "fej";
  case MSCKFUpdatePriorMatchStatus::kCameraValue:
    return "camera_value";
  }
  return "unknown";
}

MSCKFUpdatePreviewResult UpdaterMSCKFPreview::Compute(const std::shared_ptr<State> &state,
                                                      const std::vector<std::shared_ptr<ov_type::Type>> &H_order,
                                                      const Eigen::MatrixXd &H, const Eigen::VectorXd &residual,
                                                      const Eigen::MatrixXd &R) {
  MSCKFUpdatePreviewResult result;
  result.diagnostics.measurement_dimension = residual.rows();
  result.diagnostics.jacobian_dimension = H.cols();

  if (!state) {
    result.diagnostics.status = MSCKFUpdatePreviewStatus::kInvalidInput;
    result.diagnostics.stage = MSCKFUpdatePreviewStage::kState;
    return result;
  }

  // This adapter is the only live-object surface. It takes owning value
  // copies, records the exact StateHelper block orders, and delegates all
  // validation and arithmetic to the pointer-free kernel below.
  const MSCKFUpdatePreviewSnapshot snapshot = CaptureSnapshot(state);

  std::vector<MSCKFUpdatePreviewBlock> jacobian_layout;
  jacobian_layout.reserve(H_order.size());
  Eigen::Index ordered_column = 0;
  for (const auto &variable : H_order) {
    if (!variable) {
      jacobian_layout.push_back({-1, 0, ordered_column});
      continue;
    }
    jacobian_layout.push_back({variable->id(), variable->size(), ordered_column});
    ordered_column += variable->size();
  }

  return ComputeFromSnapshot(snapshot, jacobian_layout, H, residual, R);
}

MSCKFUpdatePreviewSnapshot
UpdaterMSCKFPreview::CaptureSnapshot(const std::shared_ptr<State> &state) {
  MSCKFUpdatePreviewSnapshot snapshot;
  if (!state) {
    return snapshot;
  }
  snapshot.covariance = state->_Cov;
  snapshot.state_blocks.reserve(state->_variables.size());
  for (const auto &variable : state->_variables) {
    if (!variable) {
      snapshot.state_blocks.push_back({-1, 0, -1});
      continue;
    }
    snapshot.state_blocks.push_back({variable->id(), variable->size(), variable->id()});
  }
  return snapshot;
}

MSCKFUpdatePriorSnapshot
UpdaterMSCKFPreview::CapturePrior(const std::shared_ptr<State> &state) {
  MSCKFUpdatePriorSnapshot snapshot;
  if (!state) {
    return snapshot;
  }

  snapshot.filter = CaptureSnapshot(state);
  snapshot.timestamp = state->_timestamp;
  snapshot.do_fej = state->_options.do_fej;
  snapshot.calibrate_camera_pose =
      state->_options.do_calib_camera_pose;
  snapshot.calibrate_camera_intrinsics =
      state->_options.do_calib_camera_intrinsics;
  snapshot.calibrate_camera_timeoffset =
      state->_options.do_calib_camera_timeoffset;
  snapshot.feature_representation =
      static_cast<int>(state->_options.feat_rep_msckf);
  snapshot.nominal_blocks.reserve(state->_variables.size());
  for (const auto &variable : state->_variables) {
    MSCKFUpdatePriorNominalBlock block;
    if (variable) {
      block.covariance_id = variable->id();
      block.size = variable->size();
      block.value = variable->value();
      block.fej = variable->fej();
    }
    snapshot.nominal_blocks.push_back(std::move(block));
  }

  snapshot.clone_bindings.reserve(state->_clones_IMU.size());
  for (const auto &clone : state->_clones_IMU) {
    snapshot.clone_bindings.push_back(
        {clone.first, clone.second ? clone.second->id() : -1});
  }

  snapshot.cameras.reserve(state->_calib_IMUtoCAM.size());
  for (const auto &calibration : state->_calib_IMUtoCAM) {
    MSCKFUpdatePriorCamera camera;
    camera.camera_id = calibration.first;
    const auto intrinsic = state->_cam_intrinsics.find(calibration.first);
    const auto cache = state->_cam_intrinsics_cameras.find(calibration.first);
    if (calibration.second) {
      camera.extrinsic_id = calibration.second->id();
      camera.extrinsic_value = calibration.second->value();
      camera.extrinsic_fej = calibration.second->fej();
    }
    if (intrinsic != state->_cam_intrinsics.end() && intrinsic->second) {
      camera.intrinsic_id = intrinsic->second->id();
      camera.intrinsic_value = intrinsic->second->value();
      camera.intrinsic_fej = intrinsic->second->fej();
    }
    if (cache != state->_cam_intrinsics_cameras.end() && cache->second) {
      camera.cache_value = cache->second->get_value();
      camera.width = cache->second->w();
      camera.height = cache->second->h();
      camera.model = camera_model(cache->second);
    }
    snapshot.cameras.push_back(std::move(camera));
  }
  std::sort(snapshot.cameras.begin(), snapshot.cameras.end(),
            [](const MSCKFUpdatePriorCamera &left,
               const MSCKFUpdatePriorCamera &right) {
              return left.camera_id < right.camera_id;
            });
  return snapshot;
}

MSCKFUpdatePriorMatchResult UpdaterMSCKFPreview::MatchPrior(
    const std::shared_ptr<State> &state,
    const MSCKFUpdatePriorSnapshot &snapshot) {
  MSCKFUpdatePriorMatchResult result;
  if (!state || snapshot.filter.covariance.rows() <= 0 ||
      snapshot.filter.covariance.cols() != snapshot.filter.covariance.rows()) {
    return result;
  }

  if (state->_Cov.rows() != snapshot.filter.covariance.rows() ||
      state->_Cov.cols() != snapshot.filter.covariance.cols() ||
      state->_variables.size() != snapshot.filter.state_blocks.size() ||
      state->_variables.size() != snapshot.nominal_blocks.size()) {
    result.status = MSCKFUpdatePriorMatchStatus::kCovarianceLayout;
    return result;
  }
  for (std::size_t index = 0; index < state->_variables.size(); ++index) {
    const auto &variable = state->_variables[index];
    const auto &layout = snapshot.filter.state_blocks[index];
    const auto &nominal = snapshot.nominal_blocks[index];
    if (!variable || variable->id() != layout.covariance_id ||
        variable->size() != layout.size || layout.offset != layout.covariance_id ||
        nominal.covariance_id != layout.covariance_id ||
        nominal.size != layout.size) {
      result.status = MSCKFUpdatePriorMatchStatus::kCovarianceLayout;
      result.offending_index = static_cast<Eigen::Index>(index);
      return result;
    }
    if (!type_storage_consistent(variable, false)) {
      result.status = MSCKFUpdatePriorMatchStatus::kNominal;
      result.offending_index = static_cast<Eigen::Index>(index);
      return result;
    }
    if (!type_storage_consistent(variable, true)) {
      result.status = MSCKFUpdatePriorMatchStatus::kFej;
      result.offending_index = static_cast<Eigen::Index>(index);
      return result;
    }
  }

  if (state->_clones_IMU.size() != snapshot.clone_bindings.size()) {
    result.status = MSCKFUpdatePriorMatchStatus::kCloneLayout;
    return result;
  }
  std::size_t clone_index = 0;
  for (const auto &clone : state->_clones_IMU) {
    const auto &binding = snapshot.clone_bindings[clone_index];
    if (!clone.second || clone.first != binding.timestamp ||
        clone.second->id() != binding.covariance_id ||
        !pose_storage_consistent(*clone.second, false) ||
        !pose_storage_consistent(*clone.second, true)) {
      result.status = MSCKFUpdatePriorMatchStatus::kCloneLayout;
      result.offending_index = static_cast<Eigen::Index>(clone_index);
      return result;
    }
    ++clone_index;
  }

  if (state->_calib_IMUtoCAM.size() != snapshot.cameras.size() ||
      state->_cam_intrinsics.size() != snapshot.cameras.size() ||
      state->_cam_intrinsics_cameras.size() != snapshot.cameras.size()) {
    result.status = MSCKFUpdatePriorMatchStatus::kCameraLayout;
    return result;
  }
  if (state->_options.do_fej != snapshot.do_fej ||
      state->_options.do_calib_camera_pose !=
          snapshot.calibrate_camera_pose ||
      state->_options.do_calib_camera_intrinsics !=
          snapshot.calibrate_camera_intrinsics ||
      state->_options.do_calib_camera_timeoffset !=
          snapshot.calibrate_camera_timeoffset ||
      static_cast<int>(state->_options.feat_rep_msckf) !=
          snapshot.feature_representation) {
    result.status = MSCKFUpdatePriorMatchStatus::kOptions;
    return result;
  }
  if (state->_timestamp != snapshot.timestamp) {
    result.status = MSCKFUpdatePriorMatchStatus::kTimestamp;
    return result;
  }
  if (!matrix_binary_equal(state->_Cov, snapshot.filter.covariance)) {
    result.status = MSCKFUpdatePriorMatchStatus::kCovariance;
    return result;
  }
  for (std::size_t index = 0; index < state->_variables.size(); ++index) {
    if (!matrix_binary_equal(state->_variables[index]->value(),
                             snapshot.nominal_blocks[index].value)) {
      result.status = MSCKFUpdatePriorMatchStatus::kNominal;
      result.offending_index = static_cast<Eigen::Index>(index);
      return result;
    }
    if (!matrix_binary_equal(state->_variables[index]->fej(),
                             snapshot.nominal_blocks[index].fej)) {
      result.status = MSCKFUpdatePriorMatchStatus::kFej;
      result.offending_index = static_cast<Eigen::Index>(index);
      return result;
    }
  }

  for (std::size_t index = 0; index < snapshot.cameras.size(); ++index) {
    const auto &expected = snapshot.cameras[index];
    const auto extrinsic = state->_calib_IMUtoCAM.find(expected.camera_id);
    const auto intrinsic = state->_cam_intrinsics.find(expected.camera_id);
    const auto cache = state->_cam_intrinsics_cameras.find(expected.camera_id);
    if (extrinsic == state->_calib_IMUtoCAM.end() || !extrinsic->second ||
        intrinsic == state->_cam_intrinsics.end() || !intrinsic->second ||
        cache == state->_cam_intrinsics_cameras.end() || !cache->second ||
        extrinsic->second->id() != expected.extrinsic_id ||
        intrinsic->second->id() != expected.intrinsic_id) {
      result.status = MSCKFUpdatePriorMatchStatus::kCameraLayout;
      result.offending_index = static_cast<Eigen::Index>(index);
      return result;
    }
    if (!pose_storage_consistent(*extrinsic->second, false)) {
      result.status = MSCKFUpdatePriorMatchStatus::kNominal;
      result.offending_index = static_cast<Eigen::Index>(index);
      return result;
    }
    if (!pose_storage_consistent(*extrinsic->second, true)) {
      result.status = MSCKFUpdatePriorMatchStatus::kFej;
      result.offending_index = static_cast<Eigen::Index>(index);
      return result;
    }
    if (!matrix_binary_equal(extrinsic->second->value(),
                             expected.extrinsic_value) ||
        !matrix_binary_equal(extrinsic->second->fej(),
                             expected.extrinsic_fej) ||
        !matrix_binary_equal(intrinsic->second->value(),
                             expected.intrinsic_value) ||
        !matrix_binary_equal(intrinsic->second->fej(),
                             expected.intrinsic_fej) ||
        !matrix_binary_equal(cache->second->get_value(),
                             expected.cache_value) ||
        cache->second->w() != expected.width ||
        cache->second->h() != expected.height ||
        camera_model(cache->second) != expected.model) {
      result.status = MSCKFUpdatePriorMatchStatus::kCameraValue;
      result.offending_index = static_cast<Eigen::Index>(index);
      return result;
    }
  }

  result.status = MSCKFUpdatePriorMatchStatus::kAccepted;
  return result;
}

MSCKFUpdateMeanResult UpdaterMSCKFPreview::ComputeMeanFromSnapshot(
    const MSCKFUpdatePreviewSnapshot &snapshot,
    const std::vector<MSCKFUpdatePreviewBlock> &jacobian_layout,
    const Eigen::MatrixXd &H, const Eigen::VectorXd &residual,
    const Eigen::MatrixXd &R) {
  MSCKFUpdateMeanResult result;
  result.diagnostics.measurement_dimension = residual.rows();
  result.diagnostics.jacobian_dimension = H.cols();

  const Eigen::MatrixXd &P = snapshot.covariance;
  result.diagnostics.state_dimension = P.rows();

  const Eigen::Index n = P.rows();
  const Eigen::Index m = residual.rows();
  if (n <= 0 || m <= 0 || P.cols() != n || H.rows() != m || H.cols() <= 0 ||
      R.rows() != m || R.cols() != m || jacobian_layout.empty()) {
    result.diagnostics.status = MSCKFUpdatePreviewStatus::kInvalidInput;
    result.diagnostics.stage = MSCKFUpdatePreviewStage::kInputDimensions;
    return result;
  }

  if (snapshot.state_blocks.empty()) {
    result.diagnostics.offending_order_index = 0;
    result.diagnostics.status = MSCKFUpdatePreviewStatus::kInvalidInput;
    result.diagnostics.stage = MSCKFUpdatePreviewStage::kStateOrder;
    return result;
  }
  Eigen::Index expected_state_offset = 0;
  Eigen::Index previous_covariance_id = -1;
  for (std::size_t block_index = 0;
       block_index < snapshot.state_blocks.size(); ++block_index) {
    const MSCKFUpdatePreviewBlock &block = snapshot.state_blocks[block_index];
    const bool invalid_range =
        block.covariance_id < 0 || block.size <= 0 || block.offset < 0 ||
        block.offset > n - block.size ||
        block.covariance_id != block.offset;
    const bool invalid_order =
        block.offset != expected_state_offset ||
        (block_index > 0 &&
         block.covariance_id <= previous_covariance_id);
    if (invalid_range || invalid_order) {
      result.diagnostics.offending_order_index =
          static_cast<Eigen::Index>(block_index);
      result.diagnostics.status = MSCKFUpdatePreviewStatus::kInvalidInput;
      result.diagnostics.stage = MSCKFUpdatePreviewStage::kStateOrder;
      return result;
    }
    expected_state_offset += block.size;
    previous_covariance_id = block.covariance_id;
  }
  if (expected_state_offset != n) {
    result.diagnostics.offending_order_index =
        static_cast<Eigen::Index>(snapshot.state_blocks.size());
    result.diagnostics.status = MSCKFUpdatePreviewStatus::kInvalidInput;
    result.diagnostics.stage = MSCKFUpdatePreviewStage::kStateOrder;
    return result;
  }

  Eigen::Index ordered_column = 0;
  std::vector<unsigned char> covered(static_cast<std::size_t>(n), 0);
  for (std::size_t order_index = 0;
       order_index < jacobian_layout.size(); ++order_index) {
    const MSCKFUpdatePreviewBlock &block = jacobian_layout[order_index];
    if (block.covariance_id < 0 || block.size <= 0 ||
        block.offset != ordered_column ||
        block.covariance_id > n - block.size ||
        block.offset > H.cols() - block.size) {
      result.diagnostics.offending_order_index =
          static_cast<Eigen::Index>(order_index);
      result.diagnostics.status = MSCKFUpdatePreviewStatus::kInvalidInput;
      result.diagnostics.stage = MSCKFUpdatePreviewStage::kStateOrder;
      return result;
    }

    bool contained_by_state_block = false;
    for (const MSCKFUpdatePreviewBlock &state_block :
         snapshot.state_blocks) {
      if (block.covariance_id >= state_block.offset &&
          block.covariance_id <=
              state_block.offset + state_block.size - block.size) {
        contained_by_state_block = true;
        break;
      }
    }
    if (!contained_by_state_block) {
      result.diagnostics.offending_order_index =
          static_cast<Eigen::Index>(order_index);
      result.diagnostics.status = MSCKFUpdatePreviewStatus::kInvalidInput;
      result.diagnostics.stage = MSCKFUpdatePreviewStage::kStateOrder;
      return result;
    }

    for (Eigen::Index covariance_offset = 0;
         covariance_offset < block.size; ++covariance_offset) {
      const std::size_t covariance_index = static_cast<std::size_t>(
          block.covariance_id + covariance_offset);
      if (covered[covariance_index] != 0) {
        result.diagnostics.offending_order_index =
            static_cast<Eigen::Index>(order_index);
        result.diagnostics.status = MSCKFUpdatePreviewStatus::kInvalidInput;
        result.diagnostics.stage = MSCKFUpdatePreviewStage::kStateOrder;
        return result;
      }
      covered[covariance_index] = 1;
    }
    ordered_column += block.size;
  }
  result.diagnostics.ordered_jacobian_dimension = ordered_column;
  if (ordered_column != H.cols()) {
    result.diagnostics.status = MSCKFUpdatePreviewStatus::kInvalidInput;
    result.diagnostics.stage = MSCKFUpdatePreviewStage::kInputDimensions;
    return result;
  }

  if (!P.allFinite() || !H.allFinite() || !residual.allFinite() ||
      !R.allFinite()) {
    result.diagnostics.status = MSCKFUpdatePreviewStatus::kNonfinite;
    result.diagnostics.stage = MSCKFUpdatePreviewStage::kRawInputs;
    return result;
  }

  Eigen::MatrixXd M = Eigen::MatrixXd::Zero(n, m);
  for (const MSCKFUpdatePreviewBlock &state_block :
       snapshot.state_blocks) {
    Eigen::MatrixXd M_block =
        Eigen::MatrixXd::Zero(state_block.size, m);
    for (const MSCKFUpdatePreviewBlock &measurement_block :
         jacobian_layout) {
      M_block.noalias() +=
          P.block(state_block.offset, measurement_block.covariance_id,
                  state_block.size, measurement_block.size) *
          H.block(0, measurement_block.offset, m,
                  measurement_block.size)
              .transpose();
    }
    M.block(state_block.offset, 0, state_block.size, m) = M_block;
  }
  if (!M.allFinite()) {
    result.diagnostics.status = MSCKFUpdatePreviewStatus::kNonfinite;
    result.diagnostics.stage = MSCKFUpdatePreviewStage::kCrossCovariance;
    return result;
  }

  Eigen::MatrixXd P_small(H.cols(), H.cols());
  Eigen::Index row = 0;
  for (const MSCKFUpdatePreviewBlock &row_block : jacobian_layout) {
    Eigen::Index col = 0;
    for (const MSCKFUpdatePreviewBlock &column_block : jacobian_layout) {
      P_small.block(row, col, row_block.size, column_block.size) =
          P.block(row_block.covariance_id,
                  column_block.covariance_id, row_block.size,
                  column_block.size);
      col += column_block.size;
    }
    row += row_block.size;
  }
  if (!P_small.allFinite()) {
    result.diagnostics.status = MSCKFUpdatePreviewStatus::kNonfinite;
    result.diagnostics.stage = MSCKFUpdatePreviewStage::kMarginalCovariance;
    return result;
  }

  Eigen::MatrixXd S(m, m);
  S.triangularView<Eigen::Upper>() = H * P_small * H.transpose();
  S.triangularView<Eigen::Upper>() += R;
  if (!S.triangularView<Eigen::Upper>().toDenseMatrix().allFinite()) {
    result.diagnostics.status = MSCKFUpdatePreviewStatus::kNonfinite;
    result.diagnostics.stage = MSCKFUpdatePreviewStage::kInnovation;
    return result;
  }

  Eigen::LLT<Eigen::MatrixXd, Eigen::Upper> innovation_llt(S);
  if (innovation_llt.info() != Eigen::Success) {
    result.diagnostics.status =
        MSCKFUpdatePreviewStatus::kFactorizationFailed;
    result.diagnostics.stage =
        MSCKFUpdatePreviewStage::kInnovationFactorization;
    return result;
  }

  Eigen::MatrixXd Sinv = Eigen::MatrixXd::Identity(m, m);
  innovation_llt.solveInPlace(Sinv);
  if (innovation_llt.info() != Eigen::Success || !Sinv.allFinite()) {
    result.diagnostics.status =
        innovation_llt.info() == Eigen::Success
            ? MSCKFUpdatePreviewStatus::kNonfinite
            : MSCKFUpdatePreviewStatus::kFactorizationFailed;
    result.diagnostics.stage = MSCKFUpdatePreviewStage::kInnovationInverse;
    return result;
  }

  const Eigen::MatrixXd K = M * Sinv.selfadjointView<Eigen::Upper>();
  if (!K.allFinite()) {
    result.diagnostics.status = MSCKFUpdatePreviewStatus::kNonfinite;
    result.diagnostics.stage = MSCKFUpdatePreviewStage::kKalmanGain;
    return result;
  }

  const Eigen::VectorXd dx = K * residual;
  const Eigen::VectorXd solved_residual =
      Sinv.selfadjointView<Eigen::Upper>() * residual;
  const double nis = residual.dot(solved_residual);
  if (!dx.allFinite() || !solved_residual.allFinite() ||
      !std::isfinite(nis)) {
    result.diagnostics.status = MSCKFUpdatePreviewStatus::kNonfinite;
    result.diagnostics.stage = MSCKFUpdatePreviewStage::kStateIncrement;
    return result;
  }

  result.dx = dx;
  result.nis = nis;
  result.diagnostics.status = MSCKFUpdatePreviewStatus::kAccepted;
  result.diagnostics.stage = MSCKFUpdatePreviewStage::kAccepted;
  return result;
}

MSCKFUpdatePreviewResult UpdaterMSCKFPreview::ComputeFromSnapshot(
    const MSCKFUpdatePreviewSnapshot &snapshot,
    const std::vector<MSCKFUpdatePreviewBlock> &jacobian_layout,
    const Eigen::MatrixXd &H, const Eigen::VectorXd &residual,
    const Eigen::MatrixXd &R) {
  MSCKFUpdatePreviewResult result;
  result.diagnostics.measurement_dimension = residual.rows();
  result.diagnostics.jacobian_dimension = H.cols();

  const Eigen::MatrixXd &P = snapshot.covariance;
  result.diagnostics.state_dimension = P.rows();

  const Eigen::Index n = P.rows();
  const Eigen::Index m = residual.rows();
  if (n <= 0 || m <= 0 || P.cols() != n || H.rows() != m || H.cols() <= 0 ||
      R.rows() != m || R.cols() != m || jacobian_layout.empty()) {
    result.diagnostics.status = MSCKFUpdatePreviewStatus::kInvalidInput;
    result.diagnostics.stage = MSCKFUpdatePreviewStage::kInputDimensions;
    return result;
  }

  // The owning snapshot must describe every covariance coordinate exactly
  // once, in increasing ID and offset order. This is the value-only analogue
  // of State::_variables and fixes the block loop order used by EKFUpdate.
  if (snapshot.state_blocks.empty()) {
    result.diagnostics.offending_order_index = 0;
    result.diagnostics.status = MSCKFUpdatePreviewStatus::kInvalidInput;
    result.diagnostics.stage = MSCKFUpdatePreviewStage::kStateOrder;
    return result;
  }
  Eigen::Index expected_state_offset = 0;
  Eigen::Index previous_covariance_id = -1;
  for (std::size_t block_index = 0; block_index < snapshot.state_blocks.size(); ++block_index) {
    const MSCKFUpdatePreviewBlock &block = snapshot.state_blocks[block_index];
    const bool invalid_range = block.covariance_id < 0 || block.size <= 0 || block.offset < 0 ||
                               block.offset > n - block.size || block.covariance_id != block.offset;
    const bool invalid_order = block.offset != expected_state_offset ||
                               (block_index > 0 && block.covariance_id <= previous_covariance_id);
    if (invalid_range || invalid_order) {
      result.diagnostics.offending_order_index = static_cast<Eigen::Index>(block_index);
      result.diagnostics.status = MSCKFUpdatePreviewStatus::kInvalidInput;
      result.diagnostics.stage = MSCKFUpdatePreviewStage::kStateOrder;
      return result;
    }
    expected_state_offset += block.size;
    previous_covariance_id = block.covariance_id;
  }
  if (expected_state_offset != n) {
    result.diagnostics.offending_order_index =
        static_cast<Eigen::Index>(snapshot.state_blocks.size());
    result.diagnostics.status = MSCKFUpdatePreviewStatus::kInvalidInput;
    result.diagnostics.stage = MSCKFUpdatePreviewStage::kStateOrder;
    return result;
  }

  // Validate every compressed-Jacobian block before any Eigen block access.
  // The layout may select a top-level state block or any contained
  // subvariable, but it must be injective in covariance coordinates and must
  // account for every column of H exactly.
  Eigen::Index ordered_column = 0;
  std::vector<unsigned char> covered(static_cast<std::size_t>(n), 0);
  for (std::size_t order_index = 0; order_index < jacobian_layout.size(); ++order_index) {
    const MSCKFUpdatePreviewBlock &block = jacobian_layout[order_index];
    if (block.covariance_id < 0 || block.size <= 0 || block.offset != ordered_column ||
        block.covariance_id > n - block.size || block.offset > H.cols() - block.size) {
      result.diagnostics.offending_order_index = static_cast<Eigen::Index>(order_index);
      result.diagnostics.status = MSCKFUpdatePreviewStatus::kInvalidInput;
      result.diagnostics.stage = MSCKFUpdatePreviewStage::kStateOrder;
      return result;
    }

    bool contained_by_state_block = false;
    for (const MSCKFUpdatePreviewBlock &state_block : snapshot.state_blocks) {
      if (block.covariance_id >= state_block.offset &&
          block.covariance_id <= state_block.offset + state_block.size - block.size) {
        contained_by_state_block = true;
        break;
      }
    }
    if (!contained_by_state_block) {
      result.diagnostics.offending_order_index = static_cast<Eigen::Index>(order_index);
      result.diagnostics.status = MSCKFUpdatePreviewStatus::kInvalidInput;
      result.diagnostics.stage = MSCKFUpdatePreviewStage::kStateOrder;
      return result;
    }

    for (Eigen::Index covariance_offset = 0; covariance_offset < block.size; ++covariance_offset) {
      const std::size_t covariance_index =
          static_cast<std::size_t>(block.covariance_id + covariance_offset);
      if (covered[covariance_index] != 0) {
        result.diagnostics.offending_order_index = static_cast<Eigen::Index>(order_index);
        result.diagnostics.status = MSCKFUpdatePreviewStatus::kInvalidInput;
        result.diagnostics.stage = MSCKFUpdatePreviewStage::kStateOrder;
        return result;
      }
      covered[covariance_index] = 1;
    }
    ordered_column += block.size;
  }
  result.diagnostics.ordered_jacobian_dimension = ordered_column;
  if (ordered_column != H.cols()) {
    result.diagnostics.status = MSCKFUpdatePreviewStatus::kInvalidInput;
    result.diagnostics.stage = MSCKFUpdatePreviewStage::kInputDimensions;
    return result;
  }

  if (!P.allFinite() || !H.allFinite() || !residual.allFinite() || !R.allFinite()) {
    result.diagnostics.status = MSCKFUpdatePreviewStatus::kNonfinite;
    result.diagnostics.stage = MSCKFUpdatePreviewStage::kRawInputs;
    return result;
  }

  // Match StateHelper::EKFUpdate's block ordering exactly rather than relying
  // on a mathematically equivalent full GEMM. This retains the correction to
  // every directly unobserved state block correlated with the Jacobian layout
  // while also making the preflight's floating-point proposal track the live
  // commit.
  Eigen::MatrixXd M = Eigen::MatrixXd::Zero(n, m);
  for (const MSCKFUpdatePreviewBlock &state_block : snapshot.state_blocks) {
    Eigen::MatrixXd M_block = Eigen::MatrixXd::Zero(state_block.size, m);
    for (const MSCKFUpdatePreviewBlock &measurement_block : jacobian_layout) {
      M_block.noalias() += P.block(state_block.offset, measurement_block.covariance_id,
                                   state_block.size, measurement_block.size) *
                           H.block(0, measurement_block.offset, m,
                                   measurement_block.size)
                               .transpose();
    }
    M.block(state_block.offset, 0, state_block.size, m) = M_block;
  }
  if (!M.allFinite()) {
    result.diagnostics.status = MSCKFUpdatePreviewStatus::kNonfinite;
    result.diagnostics.stage = MSCKFUpdatePreviewStage::kCrossCovariance;
    return result;
  }

  // Copy P_small in the exact Jacobian block layout used by EKFUpdate. Using
  // one owning covariance snapshot keeps M and S internally consistent.
  Eigen::MatrixXd P_small(H.cols(), H.cols());
  Eigen::Index row = 0;
  for (const MSCKFUpdatePreviewBlock &row_block : jacobian_layout) {
    Eigen::Index col = 0;
    for (const MSCKFUpdatePreviewBlock &column_block : jacobian_layout) {
      P_small.block(row, col, row_block.size, column_block.size) =
          P.block(row_block.covariance_id, column_block.covariance_id,
                  row_block.size, column_block.size);
      col += column_block.size;
    }
    row += row_block.size;
  }
  if (!P_small.allFinite()) {
    result.diagnostics.status = MSCKFUpdatePreviewStatus::kNonfinite;
    result.diagnostics.stage = MSCKFUpdatePreviewStage::kMarginalCovariance;
    return result;
  }

  // Match StateHelper::EKFUpdate: only the upper triangle defines S and R.
  Eigen::MatrixXd S(m, m);
  S.triangularView<Eigen::Upper>() = H * P_small * H.transpose();
  S.triangularView<Eigen::Upper>() += R;
  if (!S.triangularView<Eigen::Upper>().toDenseMatrix().allFinite()) {
    result.diagnostics.status = MSCKFUpdatePreviewStatus::kNonfinite;
    result.diagnostics.stage = MSCKFUpdatePreviewStage::kInnovation;
    return result;
  }

  Eigen::LLT<Eigen::MatrixXd, Eigen::Upper> innovation_llt(S);
  if (innovation_llt.info() != Eigen::Success) {
    result.diagnostics.status = MSCKFUpdatePreviewStatus::kFactorizationFailed;
    result.diagnostics.stage = MSCKFUpdatePreviewStage::kInnovationFactorization;
    return result;
  }

  Eigen::MatrixXd Sinv = Eigen::MatrixXd::Identity(m, m);
  innovation_llt.solveInPlace(Sinv);
  if (innovation_llt.info() != Eigen::Success || !Sinv.allFinite()) {
    result.diagnostics.status = innovation_llt.info() == Eigen::Success ? MSCKFUpdatePreviewStatus::kNonfinite
                                                                        : MSCKFUpdatePreviewStatus::kFactorizationFailed;
    result.diagnostics.stage = MSCKFUpdatePreviewStage::kInnovationInverse;
    return result;
  }

  const Eigen::MatrixXd K = M * Sinv.selfadjointView<Eigen::Upper>();
  if (!K.allFinite()) {
    result.diagnostics.status = MSCKFUpdatePreviewStatus::kNonfinite;
    result.diagnostics.stage = MSCKFUpdatePreviewStage::kKalmanGain;
    return result;
  }

  // Preserve the live updater's subtractive upper-triangle update and exact
  // upper-to-lower mirror convention. No Joseph form or symmetrization repair.
  Eigen::MatrixXd P_plus = P;
  P_plus.triangularView<Eigen::Upper>() -= K * M.transpose();
  P_plus = P_plus.selfadjointView<Eigen::Upper>();
  if (!P_plus.allFinite()) {
    result.diagnostics.status = MSCKFUpdatePreviewStatus::kNonfinite;
    result.diagnostics.stage = MSCKFUpdatePreviewStage::kPosteriorCovariance;
    return result;
  }

  const Eigen::VectorXd posterior_diagonal = P_plus.diagonal();
  Eigen::Index minimum_diagonal_index = 0;
  result.diagnostics.minimum_posterior_diagonal = posterior_diagonal.minCoeff(&minimum_diagonal_index);
  result.diagnostics.minimum_posterior_diagonal_available = true;
  if (result.diagnostics.minimum_posterior_diagonal < 0.0) {
    result.diagnostics.offending_diagonal_index = minimum_diagonal_index;
    result.diagnostics.status = MSCKFUpdatePreviewStatus::kNegativeDiagonal;
    result.diagnostics.stage = MSCKFUpdatePreviewStage::kPosteriorDiagonal;
    return result;
  }

  // EKFUpdate computes the increment only after its covariance/diagonal check.
  const Eigen::VectorXd dx = K * residual;
  if (!dx.allFinite()) {
    result.diagnostics.status = MSCKFUpdatePreviewStatus::kNonfinite;
    result.diagnostics.stage = MSCKFUpdatePreviewStage::kStateIncrement;
    return result;
  }

  result.dx = dx;
  result.P_plus = P_plus;
  result.diagnostics.status = MSCKFUpdatePreviewStatus::kAccepted;
  result.diagnostics.stage = MSCKFUpdatePreviewStage::kAccepted;
  return result;
}

} // namespace ov_msckf
