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

#include "UpdaterMSCKF.h"

#include "CP2CommitBoundary.h"
#include "CP2CompositeState.h"
#include "CP2FeatureGate.h"
#include "CP2StateTraceCodec.h"
#include "CP2TimingClock.h"
#include "CP2TraceCodec.h"
#include "SchurUpdate.h"
#include "UpdaterHelper.h"
#include "UpdaterMSCKFPreview.h"

#include "cam/CamRadtan.h"
#include "feat/Feature.h"
#include "feat/FeatureInitializer.h"
#include "state/State.h"
#include "state/StateHelper.h"
#include "types/LandmarkRepresentation.h"
#include "utils/colors.h"
#include "utils/print.h"
#include "utils/quat_ops.h"

#include <boost/date_time/posix_time/posix_time.hpp>
#include <boost/math/distributions/chi_squared.hpp>

#include <Eigen/Cholesky>
#include <Eigen/Eigenvalues>

#include <algorithm>
#include <cassert>
#include <cmath>
#include <cstddef>
#include <cstdlib>
#include <cstring>
#include <exception>
#include <limits>
#include <stdexcept>
#include <type_traits>
#include <unordered_map>
#include <utility>
#include <vector>

using namespace ov_core;
using namespace ov_type;
using namespace ov_msckf;

namespace {

struct MSCKFDetachedFeatureSet {
  std::vector<std::shared_ptr<Feature>> features;
  std::vector<std::size_t> source_indices;
};

bool msckf_feature_observations_equal(const Feature &left,
                                      const Feature &right) noexcept {
  if (left.featid != right.featid ||
      left.timestamps.size() != right.timestamps.size() ||
      left.uvs.size() != right.uvs.size() ||
      left.uvs_norm.size() != right.uvs_norm.size()) {
    return false;
  }
  for (const auto &camera : left.timestamps) {
    const auto match = right.timestamps.find(camera.first);
    if (match == right.timestamps.end() ||
        camera.second.size() != match->second.size() ||
        (!camera.second.empty() &&
         std::memcmp(camera.second.data(), match->second.data(),
                     camera.second.size() * sizeof(double)) != 0)) {
      return false;
    }
  }
  const auto vectors_equal = [](
      const std::unordered_map<size_t,
                               std::vector<Eigen::VectorXf>> &left_map,
      const std::unordered_map<size_t,
                               std::vector<Eigen::VectorXf>> &right_map) {
    for (const auto &camera : left_map) {
      const auto match = right_map.find(camera.first);
      if (match == right_map.end() ||
          camera.second.size() != match->second.size()) {
        return false;
      }
      for (std::size_t index = 0; index < camera.second.size(); ++index) {
        const Eigen::VectorXf &left_value = camera.second[index];
        const Eigen::VectorXf &right_value = match->second[index];
        if (left_value.rows() != right_value.rows() ||
            left_value.cols() != right_value.cols() ||
            (left_value.size() > 0 &&
             std::memcmp(left_value.data(), right_value.data(),
                         static_cast<std::size_t>(left_value.size()) *
                             sizeof(float)) != 0)) {
          return false;
        }
      }
    }
    return true;
  };
  return vectors_equal(left.uvs, right.uvs) &&
         vectors_equal(left.uvs_norm, right.uvs_norm);
}

/**
 * Ordinary-mode feature transaction. Raw entry copies remain immutable while
 * the existing updater pipeline mutates independent working copies. The live
 * objects and caller vector are updated only by FinalizePrepared() after the
 * terminal proposal decision.
 */
class MSCKFDetachedFeatureBatch {
public:
  explicit MSCKFDetachedFeatureBatch(
      const std::vector<std::shared_ptr<Feature>> &features) {
    entries_.reserve(features.size());
    active_.reserve(features.size());
    working_index_.reserve(features.size());
    for (const auto &feature : features) {
      if (!feature) {
        throw std::invalid_argument(
            "UpdaterMSCKF received a null feature pointer");
      }
      Entry entry;
      entry.live = feature;
      entry.raw = std::make_shared<Feature>(*feature);
      entry.working = std::make_shared<Feature>(*entry.raw);
      const std::size_t index = entries_.size();
      working_index_.emplace(entry.working.get(), index);
      active_.push_back(entry.working);
      entries_.push_back(std::move(entry));
    }
  }

  std::vector<std::shared_ptr<Feature>> &active() noexcept { return active_; }

  MSCKFDetachedFeatureSet CloneActive() const {
    MSCKFDetachedFeatureSet clone;
    clone.features.reserve(active_.size());
    clone.source_indices.reserve(active_.size());
    for (const auto &working : active_) {
      const auto source = working_index_.find(working.get());
      if (!working || source == working_index_.end() ||
          source->second >= entries_.size()) {
        throw std::logic_error(
            "UpdaterMSCKF detached feature mapping is incomplete");
      }
      clone.features.push_back(std::make_shared<Feature>(*working));
      clone.source_indices.push_back(source->second);
    }
    return clone;
  }

  void AdoptGeometry(const MSCKFDetachedFeatureSet &selected) {
    if (selected.features.size() != active_.size() ||
        selected.source_indices.size() != active_.size()) {
      throw std::logic_error(
          "UpdaterMSCKF selected feature geometry has invalid size");
    }
    for (std::size_t index = 0; index < active_.size(); ++index) {
      const auto source = working_index_.find(active_[index].get());
      if (!selected.features[index] || source == working_index_.end() ||
          source->second != selected.source_indices[index] ||
          source->second >= entries_.size() ||
          !msckf_feature_observations_equal(*selected.features[index],
                                            *active_[index])) {
        throw std::logic_error(
            "UpdaterMSCKF selected feature geometry changed the locked set");
      }
      Feature &destination = *entries_[source->second].working;
      const Feature &candidate = *selected.features[index];
      destination.anchor_cam_id = candidate.anchor_cam_id;
      destination.anchor_clone_timestamp =
          candidate.anchor_clone_timestamp;
      destination.p_FinA = candidate.p_FinA;
      destination.p_FinG = candidate.p_FinG;
    }
  }

  void PrepareFinalization() {
    if (prepared_) {
      return;
    }
    if (finalized_) {
      throw std::logic_error(
          "UpdaterMSCKF feature transaction finalized more than once");
    }

    final_active_.reserve(active_.size());
    for (const auto &working : active_) {
      const auto source = working_index_.find(working.get());
      if (source == working_index_.end() || source->second >= entries_.size()) {
        throw std::logic_error(
            "UpdaterMSCKF detached feature mapping is incomplete");
      }
      final_active_.push_back(entries_[source->second].live);
    }
    prepared_ = true;
  }

  void FinalizePrepared(
      std::vector<std::shared_ptr<Feature>> &features) noexcept {
    assert(prepared_);
    assert(!finalized_);
    // Keep every rejected working copy alive until this boundary so cleaning,
    // partial initializer geometry, and delete flags match the direct path.
    for (auto &entry : entries_) {
      *entry.live = std::move(*entry.working);
    }
    features.swap(final_active_);
    finalized_ = true;
  }

private:
  struct Entry {
    std::shared_ptr<Feature> live;
    std::shared_ptr<Feature> raw;
    std::shared_ptr<Feature> working;
  };

  std::vector<Entry> entries_;
  std::vector<std::shared_ptr<Feature>> active_;
  std::vector<std::shared_ptr<Feature>> final_active_;
  std::unordered_map<const Feature *, std::size_t> working_index_;
  bool prepared_ = false;
  bool finalized_ = false;
};

static_assert(std::is_nothrow_move_assignable<Feature>::value,
              "Feature finalization must not throw after state commit");

enum class MSCKFTwoPassFailure {
  kNone,
  kSnapshot,
  kCameraModel,
  kGeometry,
  kJacobian,
  kSchur,
  kGate,
  kCompression,
  kMean,
  kPriorSupport,
  kCost,
  kCovariance,
  kException,
};

const char *msckf_two_pass_failure_name(
    MSCKFTwoPassFailure failure) noexcept {
  switch (failure) {
  case MSCKFTwoPassFailure::kNone:
    return "none";
  case MSCKFTwoPassFailure::kSnapshot:
    return "snapshot";
  case MSCKFTwoPassFailure::kCameraModel:
    return "camera_model";
  case MSCKFTwoPassFailure::kGeometry:
    return "geometry";
  case MSCKFTwoPassFailure::kJacobian:
    return "jacobian";
  case MSCKFTwoPassFailure::kSchur:
    return "schur";
  case MSCKFTwoPassFailure::kGate:
    return "gate";
  case MSCKFTwoPassFailure::kCompression:
    return "compression";
  case MSCKFTwoPassFailure::kMean:
    return "mean";
  case MSCKFTwoPassFailure::kPriorSupport:
    return "prior_support";
  case MSCKFTwoPassFailure::kCost:
    return "cost";
  case MSCKFTwoPassFailure::kCovariance:
    return "covariance";
  case MSCKFTwoPassFailure::kException:
    return "exception";
  }
  return "unknown";
}

double msckf_matrix_inf_norm(const Eigen::MatrixXd &matrix) {
  if (matrix.size() == 0) {
    return 0.0;
  }
  return matrix.cwiseAbs().rowwise().sum().maxCoeff();
}

const MSCKFUpdatePriorNominalBlock *msckf_prior_block(
    const MSCKFUpdatePriorSnapshot &prior, Eigen::Index covariance_id,
    Eigen::Index size) noexcept {
  for (const MSCKFUpdatePriorNominalBlock &block :
       prior.nominal_blocks) {
    if (block.covariance_id == covariance_id && block.size == size) {
      return &block;
    }
  }
  return nullptr;
}

bool msckf_apply_absolute_delta(
    const std::shared_ptr<Type> &variable,
    const Eigen::VectorXd &absolute_delta) {
  if (!variable) {
    return false;
  }
  if (variable->id() < 0) {
    return true;
  }
  if (variable->size() <= 0 ||
      variable->id() > absolute_delta.rows() - variable->size()) {
    return false;
  }
  variable->update(
      absolute_delta.segment(variable->id(), variable->size()));
  return variable->value().allFinite();
}

struct MSCKFVisualWorkingState {
  std::shared_ptr<State> state;
  MSCKFTwoPassFailure failure = MSCKFTwoPassFailure::kSnapshot;

  bool accepted() const noexcept {
    return failure == MSCKFTwoPassFailure::kNone && state;
  }
};

MSCKFVisualWorkingState msckf_build_visual_working_state(
    const StateOptions &entry_options,
    const MSCKFUpdatePriorSnapshot &prior,
    const Eigen::VectorXd &absolute_delta) {
  MSCKFVisualWorkingState result;
  const Eigen::Index state_dimension = prior.filter.covariance.rows();
  if (state_dimension <= 0 || prior.filter.covariance.cols() != state_dimension ||
      absolute_delta.rows() != state_dimension ||
      !absolute_delta.allFinite() ||
      static_cast<int>(entry_options.feat_rep_msckf) !=
          prior.feature_representation ||
      entry_options.do_fej != prior.do_fej ||
      entry_options.do_calib_camera_pose != prior.calibrate_camera_pose ||
      entry_options.do_calib_camera_intrinsics !=
          prior.calibrate_camera_intrinsics ||
      entry_options.do_calib_camera_timeoffset !=
          prior.calibrate_camera_timeoffset) {
    return result;
  }

  StateOptions working_options = entry_options;
  std::shared_ptr<State> working =
      std::make_shared<State>(working_options);
  working->_timestamp = prior.timestamp;
  if (working->_calib_IMUtoCAM.size() != prior.cameras.size() ||
      working->_cam_intrinsics.size() != prior.cameras.size()) {
    return result;
  }
  working->_cam_intrinsics_cameras.clear();

  for (const MSCKFUpdatePriorCamera &camera : prior.cameras) {
    const auto extrinsic =
        working->_calib_IMUtoCAM.find(camera.camera_id);
    const auto intrinsic =
        working->_cam_intrinsics.find(camera.camera_id);
    if (camera.model != MSCKFUpdatePriorCameraModel::kRadtan) {
      result.failure = MSCKFTwoPassFailure::kCameraModel;
      return result;
    }
    if (extrinsic == working->_calib_IMUtoCAM.end() ||
        intrinsic == working->_cam_intrinsics.end() || !extrinsic->second ||
        !intrinsic->second || camera.extrinsic_value.rows() != 7 ||
        camera.extrinsic_value.cols() != 1 ||
        camera.extrinsic_fej.rows() != 7 || camera.extrinsic_fej.cols() != 1 ||
        camera.intrinsic_value.rows() != 8 ||
        camera.intrinsic_value.cols() != 1 ||
        camera.intrinsic_fej.rows() != 8 || camera.intrinsic_fej.cols() != 1 ||
        camera.cache_value.rows() != 8 || camera.cache_value.cols() != 1 ||
        !camera.extrinsic_value.allFinite() ||
        !camera.extrinsic_fej.allFinite() ||
        !camera.intrinsic_value.allFinite() ||
        !camera.intrinsic_fej.allFinite() ||
        !camera.cache_value.allFinite() || camera.width <= 0 ||
        camera.height <= 0 ||
        !(camera.cache_value.array() == camera.intrinsic_value.array()).all()) {
      return result;
    }

    extrinsic->second->set_local_id(camera.extrinsic_id);
    extrinsic->second->set_value(camera.extrinsic_value);
    extrinsic->second->set_fej(camera.extrinsic_fej);
    intrinsic->second->set_local_id(camera.intrinsic_id);
    intrinsic->second->set_value(camera.intrinsic_value);
    intrinsic->second->set_fej(camera.intrinsic_fej);
    if (!msckf_apply_absolute_delta(extrinsic->second, absolute_delta) ||
        !msckf_apply_absolute_delta(intrinsic->second, absolute_delta)) {
      return result;
    }
    std::shared_ptr<CamRadtan> cache =
        std::make_shared<CamRadtan>(camera.width, camera.height);
    cache->set_value(intrinsic->second->value());
    working->_cam_intrinsics_cameras.emplace(camera.camera_id,
                                              std::move(cache));
  }

  working->_clones_IMU.clear();
  for (const MSCKFUpdatePriorCloneBinding &binding :
       prior.clone_bindings) {
    const MSCKFUpdatePriorNominalBlock *block =
        msckf_prior_block(prior, binding.covariance_id, 6);
    if (!block || block->value.rows() != 7 || block->value.cols() != 1 ||
        block->fej.rows() != 7 || block->fej.cols() != 1 ||
        !block->value.allFinite() || !block->fej.allFinite() ||
        !std::isfinite(binding.timestamp) ||
        working->_clones_IMU.find(binding.timestamp) !=
            working->_clones_IMU.end()) {
      return result;
    }
    std::shared_ptr<PoseJPL> clone = std::make_shared<PoseJPL>();
    clone->set_local_id(binding.covariance_id);
    clone->set_value(block->value);
    clone->set_fej(block->fej);
    if (!msckf_apply_absolute_delta(clone, absolute_delta)) {
      return result;
    }
    working->_clones_IMU.emplace(binding.timestamp, std::move(clone));
  }

  result.state = std::move(working);
  result.failure = MSCKFTwoPassFailure::kNone;
  return result;
}

std::unordered_map<
    size_t,
    std::unordered_map<double, FeatureInitializer::ClonePose>>
msckf_camera_clone_map(const std::shared_ptr<State> &state) {
  std::unordered_map<
      size_t,
      std::unordered_map<double, FeatureInitializer::ClonePose>> result;
  for (const auto &calibration : state->_calib_IMUtoCAM) {
    std::unordered_map<double, FeatureInitializer::ClonePose> camera_poses;
    for (const auto &clone : state->_clones_IMU) {
      const Eigen::Matrix3d rotation =
          calibration.second->Rot() * clone.second->Rot();
      const Eigen::Vector3d position =
          clone.second->pos() -
          rotation.transpose() * calibration.second->pos();
      camera_poses.emplace(
          clone.first, FeatureInitializer::ClonePose(rotation, position));
    }
    result.emplace(calibration.first, std::move(camera_poses));
  }
  return result;
}

UpdaterHelper::UpdaterHelperFeature msckf_helper_feature(
    const std::shared_ptr<State> &state, const Feature &feature) {
  UpdaterHelper::UpdaterHelperFeature helper;
  helper.featid = feature.featid;
  helper.uvs = feature.uvs;
  helper.uvs_norm = feature.uvs_norm;
  helper.timestamps = feature.timestamps;
  helper.feat_representation = state->_options.feat_rep_msckf;
  if (helper.feat_representation ==
      LandmarkRepresentation::Representation::ANCHORED_INVERSE_DEPTH_SINGLE) {
    helper.feat_representation = LandmarkRepresentation::Representation::
        ANCHORED_MSCKF_INVERSE_DEPTH;
  }
  if (LandmarkRepresentation::is_relative_representation(
          helper.feat_representation)) {
    helper.anchor_cam_id = feature.anchor_cam_id;
    helper.anchor_clone_timestamp = feature.anchor_clone_timestamp;
    helper.p_FinA = feature.p_FinA;
    helper.p_FinA_fej = feature.p_FinA;
  } else {
    helper.p_FinG = feature.p_FinG;
    helper.p_FinG_fej = feature.p_FinG;
  }
  return helper;
}

bool msckf_fixed_chart_system(
    const std::vector<std::shared_ptr<Type>> &order,
    const Eigen::VectorXd &absolute_delta, const Eigen::MatrixXd &H_x,
    const Eigen::VectorXd &residual, Eigen::MatrixXd &fixed_H,
    Eigen::VectorXd &corrected_residual, double &correction_norm,
    std::vector<MSCKFUpdatePreviewBlock> &layout) {
  if (H_x.rows() != residual.rows() || H_x.cols() <= 0 ||
      !H_x.allFinite() || !residual.allFinite()) {
    return false;
  }
  Eigen::VectorXd local_delta = Eigen::VectorXd::Zero(H_x.cols());
  Eigen::MatrixXd chart =
      Eigen::MatrixXd::Identity(H_x.cols(), H_x.cols());
  layout.clear();
  layout.reserve(order.size());
  Eigen::Index offset = 0;
  for (const auto &variable : order) {
    if (!variable || variable->id() < 0 || variable->size() <= 0 ||
        offset > H_x.cols() - variable->size() ||
        variable->id() > absolute_delta.rows() - variable->size()) {
      return false;
    }
    local_delta.segment(offset, variable->size()) =
        absolute_delta.segment(variable->id(), variable->size());
    if (std::dynamic_pointer_cast<PoseJPL>(variable)) {
      const Eigen::Vector3d orientation_delta =
          absolute_delta.segment<3>(variable->id());
      chart.block<3, 3>(offset, offset) =
          (Eigen::Matrix3d::Identity() -
           0.5 * skew_x(orientation_delta)) /
          (1.0 + 0.25 * orientation_delta.squaredNorm());
    }
    layout.push_back(
        {variable->id(), variable->size(), offset});
    offset += variable->size();
  }
  if (offset != H_x.cols()) {
    return false;
  }
  fixed_H = H_x * chart;
  const Eigen::VectorXd correction = fixed_H * local_delta;
  corrected_residual = residual + correction;
  correction_norm = correction.norm();
  return fixed_H.allFinite() && corrected_residual.allFinite() &&
         std::isfinite(correction_norm);
}

struct MSCKFPriorSupportFactor {
  bool valid = false;
  Eigen::MatrixXd basis;
  Eigen::VectorXd positive_eigenvalues;
  double zero_tolerance = std::numeric_limits<double>::quiet_NaN();
  double minimum_eigenvalue = std::numeric_limits<double>::quiet_NaN();
  double maximum_eigenvalue = std::numeric_limits<double>::quiet_NaN();

  bool Evaluate(const Eigen::VectorXd &delta, double &cost,
                double &support_error) const {
    if (!valid || delta.rows() != basis.rows() || !delta.allFinite()) {
      return false;
    }
    const Eigen::VectorXd projected = basis.transpose() * delta;
    const Eigen::VectorXd supported = basis * projected;
    support_error = (delta - supported).norm();
    const double support_tolerance =
        256.0 * static_cast<double>(std::max<Eigen::Index>(1, delta.rows())) *
        std::numeric_limits<double>::epsilon() *
        std::max(1.0, delta.norm());
    if (!std::isfinite(support_error) ||
        support_error > support_tolerance) {
      return false;
    }
    if (positive_eigenvalues.size() == 0) {
      cost = 0.0;
      return true;
    }
    const Eigen::VectorXd coordinate =
        projected.array() / positive_eigenvalues.array().sqrt();
    cost = 0.5 * coordinate.squaredNorm();
    return coordinate.allFinite() && std::isfinite(cost);
  }
};

MSCKFPriorSupportFactor msckf_factor_prior_support(
    const Eigen::MatrixXd &prior) {
  MSCKFPriorSupportFactor result;
  if (prior.rows() <= 0 || prior.cols() != prior.rows() ||
      !prior.allFinite()) {
    return result;
  }
  const double symmetry_error =
      msckf_matrix_inf_norm(prior - prior.transpose());
  const double prior_scale =
      std::max(1.0, msckf_matrix_inf_norm(prior));
  if (!std::isfinite(symmetry_error) ||
      symmetry_error > 1.0e-10 * prior_scale) {
    return result;
  }
  const Eigen::MatrixXd symmetric =
      0.5 * (prior + prior.transpose());
  Eigen::SelfAdjointEigenSolver<Eigen::MatrixXd> eigensolver(symmetric);
  if (eigensolver.info() != Eigen::Success ||
      !eigensolver.eigenvalues().allFinite() ||
      !eigensolver.eigenvectors().allFinite()) {
    return result;
  }
  const Eigen::VectorXd &eigenvalues = eigensolver.eigenvalues();
  result.minimum_eigenvalue = eigenvalues.minCoeff();
  result.maximum_eigenvalue = eigenvalues.maxCoeff();
  const double spectral_scale =
      std::max(1.0, result.maximum_eigenvalue);
  result.zero_tolerance =
      256.0 * static_cast<double>(prior.rows()) *
      std::numeric_limits<double>::epsilon() * spectral_scale;
  if (!std::isfinite(result.zero_tolerance) ||
      result.minimum_eigenvalue < -result.zero_tolerance) {
    return result;
  }
  Eigen::Index first_positive = 0;
  while (first_positive < eigenvalues.rows() &&
         !(eigenvalues(first_positive) > result.zero_tolerance)) {
    ++first_positive;
  }
  const Eigen::Index rank = eigenvalues.rows() - first_positive;
  if (rank > 0) {
    result.basis = eigensolver.eigenvectors().rightCols(rank);
    result.positive_eigenvalues = eigenvalues.tail(rank);
  } else {
    result.basis = Eigen::MatrixXd(prior.rows(), 0);
    result.positive_eigenvalues = Eigen::VectorXd(0);
  }
  result.valid = result.basis.allFinite() &&
                 result.positive_eigenvalues.allFinite();
  return result;
}

struct MSCKFRectangularMean {
  bool valid = false;
  Eigen::VectorXd dx;
  double nis = std::numeric_limits<double>::quiet_NaN();
};

MSCKFRectangularMean msckf_compute_rectangular_mean(
    const MSCKFUpdatePreviewSnapshot &prior,
    const MSCKFPriorSupportFactor &prior_factor,
    const std::vector<MSCKFUpdatePreviewBlock> &layout,
    const Eigen::MatrixXd &H, const Eigen::VectorXd &residual,
    double sigma_pix) {
  MSCKFRectangularMean result;
  const Eigen::Index state_dimension = prior.covariance.rows();
  if (!prior_factor.valid || prior_factor.basis.rows() != state_dimension ||
      state_dimension <= 0 || prior.covariance.cols() != state_dimension ||
      H.rows() <= 0 || H.rows() != residual.rows() || H.cols() <= 0 ||
      layout.empty() || !H.allFinite() || !residual.allFinite() ||
      !std::isfinite(sigma_pix) || !(sigma_pix > 0.0)) {
    return result;
  }
  Eigen::MatrixXd full_H =
      Eigen::MatrixXd::Zero(H.rows(), state_dimension);
  Eigen::Index expected_offset = 0;
  std::vector<unsigned char> covered(
      static_cast<std::size_t>(state_dimension), 0);
  for (const MSCKFUpdatePreviewBlock &block : layout) {
    if (block.covariance_id < 0 || block.size <= 0 ||
        block.offset != expected_offset ||
        block.offset > H.cols() - block.size ||
        block.covariance_id > state_dimension - block.size) {
      return result;
    }
    for (Eigen::Index index = 0; index < block.size; ++index) {
      const std::size_t coordinate = static_cast<std::size_t>(
          block.covariance_id + index);
      if (covered[coordinate] != 0) {
        return result;
      }
      covered[coordinate] = 1;
    }
    full_H.block(0, block.covariance_id, H.rows(), block.size) =
        H.block(0, block.offset, H.rows(), block.size);
    expected_offset += block.size;
  }
  if (expected_offset != H.cols() || !full_H.allFinite()) {
    return result;
  }
  const Eigen::Index rank = prior_factor.positive_eigenvalues.rows();
  if (rank == 0) {
    const Eigen::VectorXd whitened_residual = residual.array() / sigma_pix;
    result.dx = Eigen::VectorXd::Zero(state_dimension);
    result.nis = whitened_residual.squaredNorm();
    result.valid = whitened_residual.allFinite() &&
                   std::isfinite(result.nis);
    return result;
  }
  const Eigen::MatrixXd L =
      prior_factor.basis *
      prior_factor.positive_eigenvalues.cwiseSqrt().asDiagonal();
  const Eigen::MatrixXd W = (full_H * L).array() / sigma_pix;
  const Eigen::VectorXd whitened_residual = residual.array() / sigma_pix;
  const Eigen::MatrixXd J =
      Eigen::MatrixXd::Identity(rank, rank) + W.transpose() * W;
  if (!L.allFinite() || !W.allFinite() ||
      !whitened_residual.allFinite() || !J.allFinite()) {
    return result;
  }
  Eigen::LLT<Eigen::MatrixXd> factor(J);
  if (factor.info() != Eigen::Success) {
    return result;
  }
  const Eigen::VectorXd reduced_rhs = W.transpose() * whitened_residual;
  const Eigen::VectorXd reduced_solution = factor.solve(reduced_rhs);
  result.dx = L * reduced_solution;
  result.nis = whitened_residual.squaredNorm() -
               reduced_rhs.dot(reduced_solution);
  result.valid = factor.info() == Eigen::Success &&
                 reduced_rhs.allFinite() &&
                 reduced_solution.allFinite() && result.dx.allFinite() &&
                 std::isfinite(result.nis);
  return result;
}

bool msckf_preview_layout_from_order(
    const std::vector<std::shared_ptr<Type>> &order,
    std::vector<MSCKFUpdatePreviewBlock> &layout) {
  layout.clear();
  layout.reserve(order.size());
  Eigen::Index offset = 0;
  for (const auto &variable : order) {
    if (!variable || variable->id() < 0 || variable->size() <= 0 ||
        offset > std::numeric_limits<Eigen::Index>::max() -
                     variable->size()) {
      return false;
    }
    layout.push_back({variable->id(), variable->size(), offset});
    offset += variable->size();
  }
  return !layout.empty();
}

struct MSCKFTwoPassFeatureSystem {
  Eigen::MatrixXd H;
  Eigen::VectorXd residual;
  std::vector<MSCKFUpdatePreviewBlock> layout;
};

std::vector<CP2FeatureGateLayoutBlock>
capture_cp2_feature_layout(
    const std::vector<std::shared_ptr<Type>> &order);

struct MSCKFTwoPassLinearization {
  bool valid = false;
  MSCKFTwoPassFailure failure = MSCKFTwoPassFailure::kSnapshot;
  std::size_t feature_id = 0;
  MSCKFDetachedFeatureSet geometry;
  Eigen::MatrixXd H;
  Eigen::VectorXd residual;
  std::vector<MSCKFUpdatePreviewBlock> layout;
  MSCKFRectangularMean mean;
  Eigen::Index raw_rows = 0;
  Eigen::Index reduced_rows = 0;
  double maximum_feature_nis =
      std::numeric_limits<double>::quiet_NaN();
  double maximum_gate_threshold =
      std::numeric_limits<double>::quiet_NaN();
  double affine_correction_norm = 0.0;
  std::size_t numerical_repair_count = 0;
  SchurReductionStatus schur_status =
      SchurReductionStatus::kAccepted;
  SchurReductionStage schur_stage = SchurReductionStage::kAccepted;
  bool singular_values_available = false;
  bool singular_ratio_available = false;
  Eigen::Vector3d singular_values = Eigen::Vector3d::Constant(
      std::numeric_limits<double>::quiet_NaN());
  double singular_ratio = std::numeric_limits<double>::quiet_NaN();
};

MSCKFTwoPassLinearization msckf_build_second_pass(
    const StateOptions &entry_options,
    const MSCKFUpdatePriorSnapshot &prior,
    const Eigen::VectorXd &linearization_delta,
    MSCKFDetachedFeatureSet geometry,
    const std::shared_ptr<FeatureInitializer> &initializer,
    const UpdaterOptions &options,
    const std::map<int, double> &chi_squared_table,
    const MSCKFPriorSupportFactor &prior_factor) {
  MSCKFTwoPassLinearization result;
  result.geometry = std::move(geometry);
  if (!initializer || result.geometry.features.empty() ||
      result.geometry.features.size() !=
          result.geometry.source_indices.size()) {
    return result;
  }
  const MSCKFVisualWorkingState visual =
      msckf_build_visual_working_state(entry_options, prior,
                                       linearization_delta);
  if (!visual.accepted()) {
    result.failure = visual.failure;
    return result;
  }
  auto clones_camera = msckf_camera_clone_map(visual.state);

  std::vector<MSCKFTwoPassFeatureSystem> feature_systems;
  feature_systems.reserve(result.geometry.features.size());
  std::unordered_map<Eigen::Index, std::size_t> global_block_index;
  Eigen::Index global_columns = 0;
  Eigen::Index global_rows = 0;

  for (const auto &feature : result.geometry.features) {
    if (!feature) {
      result.failure = MSCKFTwoPassFailure::kSnapshot;
      return result;
    }
    result.feature_id = feature->featid;
    bool triangulated = false;
    if (initializer->config().triangulate_1d) {
      triangulated =
          initializer->single_triangulation_1d(feature, clones_camera);
    } else {
      triangulated =
          initializer->single_triangulation(feature, clones_camera);
    }
    const bool refined =
        !initializer->config().refine_features ||
        initializer->single_gaussnewton(feature, clones_camera);
    if (!triangulated || !refined || !feature->p_FinG.allFinite() ||
        !feature->p_FinA.allFinite()) {
      result.failure = MSCKFTwoPassFailure::kGeometry;
      return result;
    }

    UpdaterHelper::UpdaterHelperFeature helper =
        msckf_helper_feature(visual.state, *feature);
    Eigen::MatrixXd H_f;
    Eigen::MatrixXd H_x;
    Eigen::VectorXd raw_residual;
    std::vector<std::shared_ptr<Type>> order;
    UpdaterHelper::get_feature_jacobian_full(
        visual.state, helper, H_f, H_x, raw_residual, order);
    Eigen::MatrixXd fixed_H;
    Eigen::VectorXd corrected_residual;
    double correction_norm = 0.0;
    std::vector<MSCKFUpdatePreviewBlock> local_layout;
    if (H_f.cols() != 3 || H_f.rows() != H_x.rows() ||
        !H_f.allFinite() ||
        !msckf_fixed_chart_system(order, linearization_delta, H_x,
                                  raw_residual, fixed_H,
                                  corrected_residual, correction_norm,
                                  local_layout)) {
      result.failure = MSCKFTwoPassFailure::kJacobian;
      return result;
    }
    result.affine_correction_norm += correction_norm;

    SchurReductionResult reduction = SchurUpdate::Reduce(
        fixed_H, H_f, corrected_residual, options.sigma_pix);
    result.schur_status = reduction.status;
    result.schur_stage = reduction.stage;
    result.singular_values_available =
        reduction.singular_values_available;
    result.singular_ratio_available = reduction.singular_ratio_available;
    result.singular_values = reduction.singular_values;
    result.singular_ratio = reduction.singular_ratio;
    result.raw_rows += reduction.raw_rows;
    result.numerical_repair_count +=
        reduction.jitter_count + reduction.clamp_count +
        reduction.regularization_count + reduction.fallback_count;
    if (!reduction.accepted()) {
      result.failure = MSCKFTwoPassFailure::kSchur;
      return result;
    }

    const CP2FeatureGateResult feature_gate = CP2FeatureGate::Evaluate(
        CP2FeatureGateInput(
            reduction.H_reduced, reduction.residual_reduced,
            prior.filter.covariance, capture_cp2_feature_layout(order),
            options.sigma_pix_sq, options.chi2_multipler),
        chi_squared_table);
    if (!feature_gate.chi2_available ||
        !feature_gate.threshold_available ||
        !feature_gate.evidence_decision_available ||
        !std::isfinite(feature_gate.chi2) ||
        !std::isfinite(feature_gate.threshold)) {
      result.failure = MSCKFTwoPassFailure::kGate;
      return result;
    }
    if (!std::isfinite(result.maximum_feature_nis) ||
        feature_gate.chi2 > result.maximum_feature_nis) {
      result.maximum_feature_nis = feature_gate.chi2;
      result.maximum_gate_threshold = feature_gate.threshold;
    }
    if (!feature_gate.lifecycle_accept || !feature_gate.evidence_accept) {
      result.failure = MSCKFTwoPassFailure::kGate;
      return result;
    }

    MSCKFTwoPassFeatureSystem system;
    system.H = std::move(reduction.H_reduced);
    system.residual = std::move(reduction.residual_reduced);
    system.layout = std::move(local_layout);
    if (system.H.rows() <= 0 || system.H.rows() != system.residual.rows() ||
        global_rows > std::numeric_limits<Eigen::Index>::max() -
                          system.H.rows()) {
      result.failure = MSCKFTwoPassFailure::kJacobian;
      return result;
    }
    global_rows += system.H.rows();
    for (const MSCKFUpdatePreviewBlock &block : system.layout) {
      const auto existing = global_block_index.find(block.covariance_id);
      if (existing != global_block_index.end()) {
        if (result.layout[existing->second].size != block.size) {
          result.failure = MSCKFTwoPassFailure::kJacobian;
          return result;
        }
        continue;
      }
      for (const MSCKFUpdatePreviewBlock &global_block : result.layout) {
        const Eigen::Index block_end = block.covariance_id + block.size;
        const Eigen::Index global_end =
            global_block.covariance_id + global_block.size;
        if (block.covariance_id < global_end &&
            global_block.covariance_id < block_end) {
          result.failure = MSCKFTwoPassFailure::kJacobian;
          return result;
        }
      }
      if (global_columns > std::numeric_limits<Eigen::Index>::max() -
                               block.size) {
        result.failure = MSCKFTwoPassFailure::kJacobian;
        return result;
      }
      global_block_index.emplace(block.covariance_id,
                                 result.layout.size());
      result.layout.push_back(
          {block.covariance_id, block.size, global_columns});
      global_columns += block.size;
    }
    feature_systems.push_back(std::move(system));
  }

  if (global_rows <= 0 || global_columns <= 0 || result.layout.empty()) {
    result.failure = MSCKFTwoPassFailure::kCompression;
    return result;
  }
  result.H = Eigen::MatrixXd::Zero(global_rows, global_columns);
  result.residual = Eigen::VectorXd::Zero(global_rows);
  Eigen::Index row_offset = 0;
  for (const MSCKFTwoPassFeatureSystem &system : feature_systems) {
    for (const MSCKFUpdatePreviewBlock &local_block : system.layout) {
      const auto global = global_block_index.find(local_block.covariance_id);
      if (global == global_block_index.end()) {
        result.failure = MSCKFTwoPassFailure::kJacobian;
        return result;
      }
      const MSCKFUpdatePreviewBlock &global_block =
          result.layout[global->second];
      result.H.block(row_offset, global_block.offset, system.H.rows(),
                     local_block.size) =
          system.H.block(0, local_block.offset, system.H.rows(),
                         local_block.size);
    }
    result.residual.segment(row_offset, system.residual.rows()) =
        system.residual;
    row_offset += system.residual.rows();
  }
  if (row_offset != global_rows || !result.H.allFinite() ||
      !result.residual.allFinite()) {
    result.failure = MSCKFTwoPassFailure::kJacobian;
    return result;
  }
  UpdaterHelper::measurement_compress_inplace(result.H, result.residual);
  result.reduced_rows = result.residual.rows();
  if (result.H.rows() <= 0 || result.H.rows() != result.residual.rows() ||
      result.H.cols() != global_columns || !result.H.allFinite() ||
      !result.residual.allFinite()) {
    result.failure = MSCKFTwoPassFailure::kCompression;
    return result;
  }
  result.mean = msckf_compute_rectangular_mean(
      prior.filter, prior_factor, result.layout, result.H,
      result.residual, options.sigma_pix);
  if (!result.mean.valid) {
    result.failure = MSCKFTwoPassFailure::kMean;
    return result;
  }
  result.failure = MSCKFTwoPassFailure::kNone;
  result.valid = true;
  return result;
}

struct MSCKFTwoPassCost {
  bool valid = false;
  MSCKFTwoPassFailure failure = MSCKFTwoPassFailure::kCost;
  std::size_t feature_id = 0;
  double pixel = std::numeric_limits<double>::quiet_NaN();
  double posterior = std::numeric_limits<double>::quiet_NaN();
  double prior = std::numeric_limits<double>::quiet_NaN();
  double support_error = std::numeric_limits<double>::quiet_NaN();
  MSCKFDetachedFeatureSet geometry;
};

MSCKFTwoPassCost msckf_evaluate_true_cost(
    const StateOptions &entry_options,
    const MSCKFUpdatePriorSnapshot &prior,
    const Eigen::VectorXd &candidate_delta,
    MSCKFDetachedFeatureSet geometry,
    const std::shared_ptr<FeatureInitializer> &initializer,
    const UpdaterOptions &options,
    const MSCKFPriorSupportFactor &prior_factor) {
  MSCKFTwoPassCost result;
  result.geometry = std::move(geometry);
  if (!initializer || !prior_factor.valid || result.geometry.features.empty() ||
      result.geometry.features.size() !=
          result.geometry.source_indices.size()) {
    result.failure = MSCKFTwoPassFailure::kPriorSupport;
    return result;
  }
  const MSCKFVisualWorkingState visual =
      msckf_build_visual_working_state(entry_options, prior,
                                       candidate_delta);
  if (!visual.accepted()) {
    result.failure = visual.failure;
    return result;
  }
  auto clones_camera = msckf_camera_clone_map(visual.state);
  double pixel_cost = 0.0;
  for (const auto &feature : result.geometry.features) {
    if (!feature) {
      result.failure = MSCKFTwoPassFailure::kSnapshot;
      return result;
    }
    result.feature_id = feature->featid;
    bool triangulated = false;
    if (initializer->config().triangulate_1d) {
      triangulated =
          initializer->single_triangulation_1d(feature, clones_camera);
    } else {
      triangulated =
          initializer->single_triangulation(feature, clones_camera);
    }
    const bool refined =
        !initializer->config().refine_features ||
        initializer->single_gaussnewton(feature, clones_camera);
    if (!triangulated || !refined || !feature->p_FinG.allFinite() ||
        !feature->p_FinA.allFinite()) {
      result.failure = MSCKFTwoPassFailure::kGeometry;
      return result;
    }
    UpdaterHelper::UpdaterHelperFeature helper =
        msckf_helper_feature(visual.state, *feature);
    Eigen::MatrixXd H_f;
    Eigen::MatrixXd H_x;
    Eigen::VectorXd residual;
    std::vector<std::shared_ptr<Type>> order;
    UpdaterHelper::get_feature_jacobian_full(
        visual.state, helper, H_f, H_x, residual, order);
    const double feature_cost = residual.squaredNorm();
    const double updated_cost = pixel_cost + feature_cost;
    if (residual.rows() <= 0 || !residual.allFinite() ||
        !std::isfinite(feature_cost) || !std::isfinite(updated_cost)) {
      result.failure = MSCKFTwoPassFailure::kCost;
      return result;
    }
    pixel_cost = updated_cost;
  }
  double prior_cost = std::numeric_limits<double>::quiet_NaN();
  double support_error = std::numeric_limits<double>::quiet_NaN();
  if (!prior_factor.Evaluate(candidate_delta, prior_cost, support_error)) {
    result.failure = MSCKFTwoPassFailure::kPriorSupport;
    return result;
  }
  const double posterior =
      prior_cost + 0.5 * pixel_cost / options.sigma_pix_sq;
  if (!std::isfinite(pixel_cost) || !std::isfinite(posterior)) {
    result.failure = MSCKFTwoPassFailure::kCost;
    return result;
  }
  result.pixel = pixel_cost;
  result.posterior = posterior;
  result.prior = prior_cost;
  result.support_error = support_error;
  result.failure = MSCKFTwoPassFailure::kNone;
  result.valid = true;
  return result;
}

struct MSCKFPosteriorValidation {
  bool valid = false;
  double symmetry_error = std::numeric_limits<double>::quiet_NaN();
  double symmetry_bound = std::numeric_limits<double>::quiet_NaN();
  double minimum_eigenvalue = std::numeric_limits<double>::quiet_NaN();
  double maximum_eigenvalue = std::numeric_limits<double>::quiet_NaN();
  double psd_bound = std::numeric_limits<double>::quiet_NaN();
  double maximum_diagonal = std::numeric_limits<double>::quiet_NaN();
};

MSCKFPosteriorValidation msckf_validate_posterior(
    const Eigen::MatrixXd &posterior) {
  MSCKFPosteriorValidation result;
  if (posterior.rows() <= 0 || posterior.cols() != posterior.rows() ||
      !posterior.allFinite()) {
    return result;
  }
  result.symmetry_error =
      msckf_matrix_inf_norm(posterior - posterior.transpose());
  result.symmetry_bound =
      1.0e-10 * std::max(1.0, msckf_matrix_inf_norm(posterior));
  result.maximum_diagonal = posterior.diagonal().maxCoeff();
  if (!std::isfinite(result.symmetry_error) ||
      result.symmetry_error > result.symmetry_bound) {
    return result;
  }
  const Eigen::MatrixXd symmetric =
      0.5 * (posterior + posterior.transpose());
  Eigen::SelfAdjointEigenSolver<Eigen::MatrixXd> eigensolver(symmetric);
  if (eigensolver.info() != Eigen::Success ||
      !eigensolver.eigenvalues().allFinite()) {
    return result;
  }
  result.minimum_eigenvalue = eigensolver.eigenvalues().minCoeff();
  result.maximum_eigenvalue = eigensolver.eigenvalues().maxCoeff();
  result.psd_bound =
      -1.0e-10 * std::max(1.0, result.maximum_eigenvalue);
  result.valid = std::isfinite(result.minimum_eigenvalue) &&
                 std::isfinite(result.maximum_eigenvalue) &&
                 result.minimum_eigenvalue >= result.psd_bound;
  return result;
}

struct MSCKFSelectedCovariance {
  bool valid = false;
  MSCKFTwoPassFailure failure = MSCKFTwoPassFailure::kCovariance;
  Eigen::VectorXd dx;
  Eigen::MatrixXd posterior;
  MSCKFPosteriorValidation validation;
};

MSCKFSelectedCovariance msckf_compute_selected_covariance(
    const MSCKFUpdatePreviewSnapshot &prior,
    const MSCKFPriorSupportFactor &prior_factor,
    const std::vector<MSCKFUpdatePreviewBlock> &layout,
    const Eigen::MatrixXd &H, const Eigen::VectorXd &residual,
    double sigma_pix) {
  MSCKFSelectedCovariance result;
  const Eigen::Index state_dimension = prior.covariance.rows();
  if (!prior_factor.valid || prior_factor.basis.rows() != state_dimension ||
      state_dimension <= 0 || prior.covariance.cols() != state_dimension ||
      H.rows() <= 0 || H.rows() != residual.rows() || H.cols() <= 0 ||
      layout.empty() || !H.allFinite() || !residual.allFinite() ||
      !std::isfinite(sigma_pix) || !(sigma_pix > 0.0)) {
    return result;
  }
  Eigen::MatrixXd full_H =
      Eigen::MatrixXd::Zero(H.rows(), state_dimension);
  Eigen::Index expected_offset = 0;
  std::vector<unsigned char> covered(
      static_cast<std::size_t>(state_dimension), 0);
  for (const MSCKFUpdatePreviewBlock &block : layout) {
    if (block.covariance_id < 0 || block.size <= 0 ||
        block.offset != expected_offset ||
        block.offset > H.cols() - block.size ||
        block.covariance_id > state_dimension - block.size) {
      return result;
    }
    for (Eigen::Index index = 0; index < block.size; ++index) {
      const std::size_t coordinate = static_cast<std::size_t>(
          block.covariance_id + index);
      if (covered[coordinate] != 0) {
        return result;
      }
      covered[coordinate] = 1;
    }
    full_H.block(0, block.covariance_id, H.rows(), block.size) =
        H.block(0, block.offset, H.rows(), block.size);
    expected_offset += block.size;
  }
  if (expected_offset != H.cols() || !full_H.allFinite()) {
    return result;
  }

  const Eigen::Index rank = prior_factor.positive_eigenvalues.rows();
  Eigen::MatrixXd L(state_dimension, rank);
  if (rank > 0) {
    L = prior_factor.basis *
        prior_factor.positive_eigenvalues.cwiseSqrt().asDiagonal();
  }
  if (!L.allFinite()) {
    return result;
  }
  if (rank == 0) {
    result.dx = Eigen::VectorXd::Zero(state_dimension);
    const Eigen::MatrixXd raw_posterior =
        Eigen::MatrixXd::Zero(state_dimension, state_dimension);
    result.validation = msckf_validate_posterior(raw_posterior);
    result.posterior = raw_posterior;
    result.valid = result.validation.valid;
    result.failure = result.valid ? MSCKFTwoPassFailure::kNone
                                  : MSCKFTwoPassFailure::kCovariance;
    return result;
  }

  const Eigen::MatrixXd W = (full_H * L).array() / sigma_pix;
  const Eigen::VectorXd whitened_residual = residual.array() / sigma_pix;
  const Eigen::MatrixXd J =
      Eigen::MatrixXd::Identity(rank, rank) + W.transpose() * W;
  if (!W.allFinite() || !whitened_residual.allFinite() || !J.allFinite()) {
    return result;
  }
  Eigen::LLT<Eigen::MatrixXd> factor(J);
  if (factor.info() != Eigen::Success) {
    result.failure = MSCKFTwoPassFailure::kMean;
    return result;
  }
  const Eigen::VectorXd y =
      factor.solve(W.transpose() * whitened_residual);
  const Eigen::MatrixXd solved_factor_transpose =
      factor.solve(L.transpose());
  if (factor.info() != Eigen::Success || !y.allFinite() ||
      !solved_factor_transpose.allFinite()) {
    result.failure = MSCKFTwoPassFailure::kMean;
    return result;
  }
  result.dx = L * y;
  const Eigen::MatrixXd raw_posterior = L * solved_factor_transpose;
  if (!result.dx.allFinite() || !raw_posterior.allFinite()) {
    return result;
  }
  result.validation = msckf_validate_posterior(raw_posterior);
  if (!result.validation.valid) {
    return result;
  }
  result.posterior =
      0.5 * (raw_posterior + raw_posterior.transpose());
  if (!result.posterior.allFinite()) {
    return result;
  }
  result.failure = MSCKFTwoPassFailure::kNone;
  result.valid = true;
  return result;
}

bool capture_cp2_duration_ns(const CP2SteadyClockEndpoint &start,
                             const CP2SteadyClockEndpoint &end,
                             CP2LiveUpdateEvent &event) noexcept {
  event.timing_start_ns = start.nanoseconds;
  event.timing_end_ns = end.nanoseconds;
  event.timing_endpoint_valid = cp2_steady_clock_duration(
      start, end, event.duration_ns);
  return event.timing_endpoint_valid;
}

bool cp2_size_to_u64(std::size_t value, std::uint64_t &output) noexcept {
  static_assert(std::numeric_limits<std::size_t>::digits <=
                    std::numeric_limits<std::uint64_t>::digits,
                "CP2 size_t values must fit the frozen u64 schema");
  output = static_cast<std::uint64_t>(value);
  return true;
}

bool cp2_checked_add_eigen_index(Eigen::Index left, Eigen::Index right,
                                 Eigen::Index &output) noexcept {
  if (left < 0 || right < 0 ||
      right > std::numeric_limits<Eigen::Index>::max() - left) {
    output = 0;
    return false;
  }
  output = left + right;
  return true;
}

bool cp2_binary64_equal(double left, double right) noexcept {
  static_assert(sizeof(double) == sizeof(std::uint64_t) &&
                    std::numeric_limits<double>::is_iec559,
                "CP2 requires IEEE-754 binary64");
  std::uint64_t left_bits = 0U;
  std::uint64_t right_bits = 0U;
  std::memcpy(&left_bits, &left, sizeof(left_bits));
  std::memcpy(&right_bits, &right, sizeof(right_bits));
  return left_bits == right_bits;
}

bool cp2_gamma_equal(CP2GammaStatus left_status, double left,
                     CP2GammaStatus right_status, double right) noexcept {
  if (left_status != right_status) {
    return false;
  }
  // The frozen JSON value exists only for kAvailable. Not-reached and
  // nonfinite gamma values are null evidence, so their in-memory sentinel/NaN
  // payload is deliberately non-authoritative and cannot veto the baseline.
  return left_status != CP2GammaStatus::kAvailable ||
         cp2_binary64_equal(left, right);
}

template <typename Left, typename Right>
bool cp2_matrix_binary64_equal(const Eigen::MatrixBase<Left> &left,
                               const Eigen::MatrixBase<Right> &right) noexcept {
  if (left.rows() != right.rows() || left.cols() != right.cols()) {
    return false;
  }
  for (Eigen::Index row = 0; row < left.rows(); ++row) {
    for (Eigen::Index column = 0; column < left.cols(); ++column) {
      if (!cp2_binary64_equal(left.derived().coeff(row, column),
                              right.derived().coeff(row, column))) {
        return false;
      }
    }
  }
  return true;
}

bool cp2_preview_layout_equal(
    const std::vector<MSCKFUpdatePreviewBlock> &left,
    const std::vector<MSCKFUpdatePreviewBlock> &right) noexcept {
  if (left.size() != right.size()) {
    return false;
  }
  for (std::size_t index = 0; index < left.size(); ++index) {
    if (left[index].covariance_id != right[index].covariance_id ||
        left[index].size != right[index].size ||
        left[index].offset != right[index].offset) {
      return false;
    }
  }
  return true;
}

bool cp2_preview_diagnostics_equal(
    const MSCKFUpdatePreviewDiagnostics &left,
    const MSCKFUpdatePreviewDiagnostics &right) noexcept {
  return left.status == right.status && left.stage == right.stage &&
         left.state_dimension == right.state_dimension &&
         left.measurement_dimension == right.measurement_dimension &&
         left.jacobian_dimension == right.jacobian_dimension &&
         left.ordered_jacobian_dimension ==
             right.ordered_jacobian_dimension &&
         left.offending_order_index == right.offending_order_index &&
         left.offending_diagonal_index == right.offending_diagonal_index &&
         left.minimum_posterior_diagonal_available ==
             right.minimum_posterior_diagonal_available &&
         (!left.minimum_posterior_diagonal_available ||
          cp2_binary64_equal(left.minimum_posterior_diagonal,
                             right.minimum_posterior_diagonal)) &&
         left.jitter_count == right.jitter_count &&
         left.repair_count == right.repair_count &&
         left.alternate_solve_count == right.alternate_solve_count &&
         left.clamp_count == right.clamp_count &&
         left.regularization_count == right.regularization_count &&
         left.fallback_count == right.fallback_count;
}

bool cp2_compressed_baseline_equal(
    const CP2GlobalModeResult &shadow,
    const Eigen::MatrixXd &live_H,
    const Eigen::VectorXd &live_residual,
    const std::vector<MSCKFUpdatePreviewBlock> &live_layout) noexcept {
  return shadow.compressed_rows == live_H.rows() &&
         cp2_matrix_binary64_equal(shadow.H_compressed, live_H) &&
         cp2_matrix_binary64_equal(shadow.residual_compressed,
                                   live_residual) &&
         cp2_preview_layout_equal(shadow.jacobian_layout, live_layout);
}

bool cp2_preview_counters_are_zero(
    const MSCKFUpdatePreviewDiagnostics &diagnostics) noexcept {
  return diagnostics.jitter_count == 0U && diagnostics.repair_count == 0U &&
         diagnostics.alternate_solve_count == 0U &&
         diagnostics.clamp_count == 0U &&
         diagnostics.regularization_count == 0U &&
         diagnostics.fallback_count == 0U;
}

bool cp2_nullspace_reducer_counters_are_zero(
    const CP2FeatureGateCounters &counters) noexcept {
  return counters.jitter_count == 0U && counters.repair_count == 0U &&
         counters.alternate_solve_count == 0U &&
         counters.clamp_count == 0U &&
         counters.regularization_count == 0U &&
         counters.silent_fallback_count == 0U &&
         counters.fallback_count == 0U;
}

bool cp2_schur_reducer_counters_are_zero(
    const SchurReductionResult &result) noexcept {
  return result.jitter_count == 0U && result.clamp_count == 0U &&
         result.regularization_count == 0U && result.fallback_count == 0U;
}

bool cp2_shadow_math_evidence_passes(
    const CP2ShadowMathResult &result) noexcept {
  if (!result.traversal_complete || !result.raw_layouts_valid ||
      !result.nullspace_assembly_valid || !result.schur_assembly_valid ||
      result.duplicate_feature_id ||
      result.nullspace.gamma_status == CP2GammaStatus::kNonfinite ||
      result.schur.gamma_status == CP2GammaStatus::kNonfinite) {
    return false;
  }
  for (const CP2FeaturePairResult &feature : result.features) {
    if (!feature.raw_layout_valid ||
        !cp2_nullspace_reducer_counters_are_zero(feature.nullspace.counters) ||
        !cp2_schur_reducer_counters_are_zero(feature.schur) ||
        (feature.statistics_comparison_required &&
        (!feature.lambda_comparison.available ||
         !feature.lambda_comparison.passed ||
         !feature.eta_comparison.available ||
         !feature.eta_comparison.passed ||
         !feature.gamma_comparison.available ||
         !feature.gamma_comparison.passed))) {
      return false;
    }
  }
  if (!cp2_preview_counters_are_zero(result.nullspace.proposal.diagnostics)) {
    return false;
  }
  if (!cp2_preview_counters_are_zero(result.schur.proposal.diagnostics)) {
    return false;
  }
  return true;
}

CP2ProposalTracePayload
cp2_proposal_payload(const MSCKFUpdatePreviewResult &proposal) {
  CP2ProposalTracePayload payload;
  payload.dx = proposal.dx;
  payload.P_plus = proposal.P_plus;
  return payload;
}

class CP2UpdateActivityGuard {
public:
  explicit CP2UpdateActivityGuard(std::atomic<bool> &active) noexcept : active_(active) {}
  ~CP2UpdateActivityGuard() { active_.store(false, std::memory_order_release); }

  CP2UpdateActivityGuard(const CP2UpdateActivityGuard &) = delete;
  CP2UpdateActivityGuard &operator=(const CP2UpdateActivityGuard &) = delete;

private:
  std::atomic<bool> &active_;
};

#if defined(OV_MSCKF_CP2_TESTING)
// Executable protecting invariant: every nonexceptional updater exit after a
// valid start sample must have sampled exactly one terminal endpoint. The
// endpoint itself is noted at each clock boundary; this destructor observes
// only the already-completed invocation and never enters the timed interval.
class CP2UpdaterTimingExitAudit final {
public:
  CP2UpdaterTimingExitAudit(
      const std::size_t &timing_end_count,
      std::size_t &normal_exit_count,
      std::size_t &verified_timing_exit_count,
      bool &timing_exit_violation) noexcept
      : timing_end_count_(timing_end_count),
        initial_timing_end_count_(timing_end_count),
        normal_exit_count_(normal_exit_count),
        verified_timing_exit_count_(verified_timing_exit_count),
        timing_exit_violation_(timing_exit_violation) {}

  ~CP2UpdaterTimingExitAudit() noexcept {
    if (std::uncaught_exception()) {
      return;
    }
    ++normal_exit_count_;
    if (timing_end_count_ == initial_timing_end_count_ + 1U) {
      ++verified_timing_exit_count_;
      return;
    }
    timing_exit_violation_ = true;
  }

  CP2UpdaterTimingExitAudit(const CP2UpdaterTimingExitAudit &) = delete;
  CP2UpdaterTimingExitAudit &
  operator=(const CP2UpdaterTimingExitAudit &) = delete;

private:
  const std::size_t &timing_end_count_;
  const std::size_t initial_timing_end_count_;
  std::size_t &normal_exit_count_;
  std::size_t &verified_timing_exit_count_;
  bool &timing_exit_violation_;
};
#endif

std::vector<CP2FeatureGateLayoutBlock>
capture_cp2_feature_layout(const std::vector<std::shared_ptr<Type>> &order) {
  std::vector<CP2FeatureGateLayoutBlock> layout;
  layout.reserve(order.size());
  Eigen::Index H_offset = 0;
  for (const std::shared_ptr<Type> &variable : order) {
    if (!variable) {
      layout.push_back({-1, 0, H_offset});
      continue;
    }
    if (variable->size() < 0) {
      throw std::overflow_error("CP2 feature layout has negative type size");
    }
    layout.push_back({variable->id(), variable->size(), H_offset});
    Eigen::Index updated_offset = 0;
    if (!cp2_checked_add_eigen_index(H_offset, variable->size(),
                                     updated_offset)) {
      throw std::overflow_error("CP2 feature layout offset overflows Eigen::Index");
    }
    H_offset = updated_offset;
  }
  return layout;
}

std::vector<MSCKFUpdatePreviewBlock>
capture_cp2_preview_layout(const std::vector<std::shared_ptr<Type>> &order) {
  std::vector<MSCKFUpdatePreviewBlock> layout;
  layout.reserve(order.size());
  Eigen::Index H_offset = 0;
  for (const std::shared_ptr<Type> &variable : order) {
    if (!variable) {
      layout.push_back({-1, 0, H_offset});
      continue;
    }
    if (variable->size() < 0) {
      throw std::overflow_error("CP2 preview layout has negative type size");
    }
    layout.push_back({variable->id(), variable->size(), H_offset});
    Eigen::Index updated_offset = 0;
    if (!cp2_checked_add_eigen_index(H_offset, variable->size(),
                                     updated_offset)) {
      throw std::overflow_error("CP2 preview layout offset overflows Eigen::Index");
    }
    H_offset = updated_offset;
  }
  return layout;
}

} // namespace

UpdaterMSCKF::UpdaterMSCKF(UpdaterOptions &options, ov_core::FeatureInitializerOptions &feat_init_options) : _options(options) {

  // Preserve the startup contract for direct/API construction as well: the
  // exact variance used by every gate and update must remain finite and >0.
  const double sigma_pix_sq = _options.sigma_pix * _options.sigma_pix;
  if (!UpdaterOptions::landmark_elimination_is_supported(_options.landmark_elimination) ||
      !UpdaterOptions::max_visual_passes_is_supported(
          _options.max_visual_passes) ||
      !UpdaterOptions::visual_pass_combination_is_supported(
          _options.max_visual_passes, _options.landmark_elimination) ||
      (_options.capture_conditioning_systems &&
       (_options.max_visual_passes != 1 ||
        _options.conditioning_capture_path.empty() ||
        _options.conditioning_capture_path.front() != '/' ||
        _options.conditioning_capture_config_path.empty())) ||
      !std::isfinite(_options.sigma_pix) || !(_options.sigma_pix > 0.0) || !std::isfinite(sigma_pix_sq) ||
      !(sigma_pix_sq > 0.0) || !std::isfinite(_options.chi2_multipler)) {
    PRINT_ERROR(RED
                "invalid MSCKF updater configuration: mode=%s sigma_px=%.17g sigma_px_sq=%.17g "
                "chi2_multiplier=%.17g max_visual_passes=%d\n" RESET,
                UpdaterOptions::landmark_elimination_as_string(_options.landmark_elimination).c_str(), _options.sigma_pix,
                sigma_pix_sq, _options.chi2_multipler,
                _options.max_visual_passes);
    std::exit(EXIT_FAILURE);
  }
  _options.sigma_pix_sq = sigma_pix_sq;

  if (_options.capture_conditioning_systems) {
    try {
      conditioning_capture_writer.reset(
          new ConditioningCaptureWriter(_options));
    } catch (...) {
      PRINT_WARNING(
          YELLOW
          "[MSCKF-CONDITIONING-CAPTURE]: status=disabled "
          "reason=owner_allocation estimator_unchanged=1\n" RESET);
    }
  }

  // Save our feature initializer
  initializer_feat = std::shared_ptr<ov_core::FeatureInitializer>(new ov_core::FeatureInitializer(feat_init_options));

  // Initialize the chi squared test table with confidence level 0.95
  // https://github.com/KumarRobotics/msckf_vio/blob/050c50defa5a7fd9a04c1eed5687b405f02919b5/src/msckf_vio.cpp#L215-L221
  for (int i = 1; i < 500; i++) {
    boost::math::chi_squared chi_squared_dist(i);
    const double quantile = boost::math::quantile(chi_squared_dist, 0.95);
    if (!std::isfinite(quantile)) {
      PRINT_ERROR(RED "invalid MSCKF chi2 constructor table: dof=%d quantile=%.17g\n" RESET,
                  i, quantile);
      std::exit(EXIT_FAILURE);
    }
    chi_squared_table.emplace(i, quantile);
  }
}

bool UpdaterMSCKF::chi2_gate_rejects(double statistic, double threshold) noexcept { return statistic > threshold; }

const char *ov_msckf::cp2_update_terminal_status_name(CP2UpdateTerminalStatus status) noexcept {
  switch (status) {
  case CP2UpdateTerminalStatus::kEmptyInput:
    return "empty_input";
  case CP2UpdateTerminalStatus::kAllRejected:
    return "all_rejected";
  case CP2UpdateTerminalStatus::kEmptyAfterCompression:
    return "empty_after_compression";
  case CP2UpdateTerminalStatus::kPreflightRejected:
    return "preflight_rejected";
  case CP2UpdateTerminalStatus::kCommittedCounted:
    return "committed_counted";
  case CP2UpdateTerminalStatus::kInternalFailure:
    return "internal_failure";
  }
  return "unknown";
}

const char *ov_msckf::cp2_update_terminal_subreason_name(CP2UpdateTerminalSubreason subreason) noexcept {
  switch (subreason) {
  case CP2UpdateTerminalSubreason::kInputEmpty:
    return "input_empty";
  case CP2UpdateTerminalSubreason::kNoFeaturesAfterCleaning:
    return "no_features_after_cleaning";
  case CP2UpdateTerminalSubreason::kNoFeaturesAfterTriangulation:
    return "no_features_after_triangulation";
  case CP2UpdateTerminalSubreason::kNoRawSystems:
    return "no_raw_systems";
  case CP2UpdateTerminalSubreason::kAllBaselineFeaturesRejected:
    return "all_baseline_features_rejected";
  case CP2UpdateTerminalSubreason::kMeasurementCompressionEmpty:
    return "measurement_compression_empty";
  case CP2UpdateTerminalSubreason::kBaselinePreflightRejected:
    return "baseline_preflight_rejected";
  case CP2UpdateTerminalSubreason::kInvalidLiveMode:
    return "invalid_live_mode";
  case CP2UpdateTerminalSubreason::kSnapshotMismatch:
    return "snapshot_mismatch";
  case CP2UpdateTerminalSubreason::kTraceInvariantFailure:
    return "trace_invariant_failure";
  case CP2UpdateTerminalSubreason::kNone:
    return "none";
  }
  return "unknown";
}

const char *ov_msckf::cp2_recorded_sink_status_name(
    CP2RecordedSinkStatus status) noexcept {
  switch (status) {
  case CP2RecordedSinkStatus::kPublished:
    return "published";
  case CP2RecordedSinkStatus::kRejected:
    return "rejected";
  }
  return "unknown";
}

const char *ov_msckf::cp2_trace_fatal_reason_name(
    CP2TraceFatalReason reason) noexcept {
  switch (reason) {
  case CP2TraceFatalReason::kNone:
    return "none";
  case CP2TraceFatalReason::kConfigurationInvariant:
    return "configuration_invariant";
  case CP2TraceFatalReason::kPhase0Promotion:
    return "phase0_promotion";
  case CP2TraceFatalReason::kRequiredEncoding:
    return "required_encoding";
  case CP2TraceFatalReason::kShadowTrace:
    return "shadow_trace";
  case CP2TraceFatalReason::kPhase1Capture:
    return "phase1_capture";
  case CP2TraceFatalReason::kPostcommitCapture:
    return "postcommit_capture";
  case CP2TraceFatalReason::kSinkRejected:
    return "sink_rejected";
  case CP2TraceFatalReason::kArithmeticInvariant:
    return "arithmetic_invariant";
  case CP2TraceFatalReason::kPostcommitException:
    return "postcommit_exception";
  }
  return "unknown";
}

bool UpdaterMSCKF::set_cp2_update_callback(CP2UpdateCallback callback, bool enable_shadow) {
  if (enable_shadow &&
      (!callback || _options.landmark_elimination != UpdaterOptions::LandmarkElimination::NULLSPACE)) {
    return false;
  }
  std::shared_ptr<const CP2UpdateCallback> installed_callback;
  if (callback) {
    installed_callback = std::make_shared<const CP2UpdateCallback>(std::move(callback));
  }
  const std::lock_guard<std::mutex> lock(cp2_callback_mutex);
  if (cp2_callback_configuration_frozen ||
      cp2_update_active.load(std::memory_order_acquire)) {
    return false;
  }
  cp2_update_callback = std::move(installed_callback);
  cp2_shadow_enabled = enable_shadow;
  return true;
}

bool UpdaterMSCKF::set_cp2_recorded_sink(
    std::shared_ptr<CP2RecordedUpdateSink> sink) {
  if (sink && _options.landmark_elimination !=
                  UpdaterOptions::LandmarkElimination::NULLSPACE) {
    return false;
  }
  const std::lock_guard<std::mutex> lock(cp2_callback_mutex);
  if (cp2_callback_configuration_frozen ||
      cp2_update_active.load(std::memory_order_acquire)) {
    return false;
  }
  cp2_recorded_sink = std::move(sink);
  return true;
}

bool UpdaterMSCKF::set_cp2_invocation_context(
    const CP2UpdateInvocationContext &context) noexcept {
  const std::lock_guard<std::mutex> lock(cp2_callback_mutex);
  if (cp2_update_active.load(std::memory_order_acquire) ||
      cp2_invocation_context_pending || cp2_trace_fatal_latched()) {
    return false;
  }
  cp2_pending_invocation_context = context;
  cp2_invocation_context_pending = true;
  return true;
}

bool UpdaterMSCKF::cp2_trace_fatal_latched() const noexcept {
  return cp2_trace_fatal_reason() != CP2TraceFatalReason::kNone;
}

CP2TraceFatalReason UpdaterMSCKF::cp2_trace_fatal_reason() const noexcept {
  return cp2_trace_fatal_reason_value.load(std::memory_order_acquire);
}

[[noreturn]] void UpdaterMSCKF::latch_cp2_trace_fatal(
    CP2TraceFatalReason reason, const char *message) {
  CP2TraceFatalReason expected = CP2TraceFatalReason::kNone;
  cp2_trace_fatal_reason_value.compare_exchange_strong(
      expected, reason, std::memory_order_acq_rel, std::memory_order_acquire);
  const CP2TraceFatalReason retained =
      cp2_trace_fatal_reason_value.load(std::memory_order_acquire);
  throw CP2TraceFatalError(retained, message);
}

void UpdaterMSCKF::update(std::shared_ptr<State> state, std::vector<std::shared_ptr<Feature>> &feature_vec) {

  // This must remain the first operation at updater entry. The endpoint is a
  // value-only, checked std::chrono::steady_clock nanosecond sample.
  CP2SteadyClockEndpoint cp2_update_start =
      cp2_steady_clock_now();
#if defined(OV_MSCKF_CP2_TESTING)
  cp2_test_note_timing_start();
  const CP2UpdaterTimingExitAudit cp2_timing_exit_audit(
      cp2_test_timing_end_count, cp2_test_normal_exit_count,
      cp2_test_verified_timing_exit_count,
      cp2_test_timing_exit_violation);
  if (cp2_test_fault == CP2UpdaterTestFault::kStartTimingClockFailure) {
    cp2_update_start.valid = false;
  }
#endif
  // Freeze the callback/sink configuration immediately after the mandatory
  // first clock sample. This first-attempt freeze is permanent even when the
  // endpoint or a pre-existing fatal latch makes this invocation throw before
  // it can claim the active updater state.
  {
    const std::lock_guard<std::mutex> lock(cp2_callback_mutex);
    cp2_callback_configuration_frozen = true;
  }
  if (!cp2_update_start.valid) {
    latch_cp2_trace_fatal(CP2TraceFatalReason::kArithmeticInvariant,
                          "CP2 steady-clock start endpoint is unavailable");
  }
  const CP2TraceFatalReason preexisting_fatal = cp2_trace_fatal_reason();
  if (preexisting_fatal != CP2TraceFatalReason::kNone) {
    throw CP2TraceFatalError(preexisting_fatal,
                            "CP2 recorded updater entered after fatal latch");
  }
  bool expected_inactive = false;
  if (!cp2_update_active.compare_exchange_strong(expected_inactive, true, std::memory_order_acquire,
                                                  std::memory_order_relaxed)) {
    throw std::logic_error("UpdaterMSCKF::update requires serialized, nonreentrant use");
  }
  const CP2UpdateActivityGuard activity_guard(cp2_update_active);
  std::shared_ptr<const CP2UpdateCallback> update_callback;
  std::shared_ptr<CP2RecordedUpdateSink> recorded_sink;
  bool shadow_requested = false;
  bool invocation_context_supplied = false;
  bool invocation_id_overflow = false;
  CP2UpdateInvocationContext invocation_context;
  std::uint64_t invocation_id = 0U;
  {
    const std::lock_guard<std::mutex> lock(cp2_callback_mutex);
    update_callback = cp2_update_callback;
    recorded_sink = cp2_recorded_sink;
    shadow_requested = cp2_shadow_enabled;
    if (cp2_invocation_context_pending) {
      invocation_context = cp2_pending_invocation_context;
      cp2_pending_invocation_context = CP2UpdateInvocationContext{};
      cp2_invocation_context_pending = false;
      invocation_context_supplied = true;
      invocation_id = cp2_next_invocation_id;
      if (!cp2_checked_add_u64(cp2_next_invocation_id, UINT64_C(1),
                               cp2_next_invocation_id)) {
        invocation_id_overflow = true;
      }
    }
  }
  const bool recorded_mode = static_cast<bool>(recorded_sink);
  if (invocation_id_overflow) {
    if (recorded_mode) {
      latch_cp2_trace_fatal(
          CP2TraceFatalReason::kArithmeticInvariant,
          "CP2 invocation identity population exceeds u64");
    }
    throw std::overflow_error(
        "UpdaterMSCKF invocation identity population exceeds u64");
  }
  if (recorded_mode && !invocation_context_supplied) {
    latch_cp2_trace_fatal(CP2TraceFatalReason::kConfigurationInvariant,
                          "CP2 recorded update requires invocation context");
  }
  if (recorded_mode && _options.landmark_elimination !=
                           UpdaterOptions::LandmarkElimination::NULLSPACE) {
    latch_cp2_trace_fatal(CP2TraceFatalReason::kConfigurationInvariant,
                          "CP2 recorded mode requires nullspace live mode");
  }

  std::shared_ptr<CP2RecordedUpdateEvent> recorded_event;
  if (recorded_mode) {
    try {
      recorded_event = std::make_shared<CP2RecordedUpdateEvent>();
      recorded_event->raw_system_payloads.reserve(feature_vec.size());
    } catch (...) {
      latch_cp2_trace_fatal(CP2TraceFatalReason::kRequiredEncoding,
                            "CP2 could not allocate the owning update record");
    }
  }

  CP2LiveUpdateEvent update_event;
  if (invocation_context_supplied) {
    update_event.invocation_context_available = true;
    update_event.sequence_index = invocation_context.sequence_index;
    update_event.pair_index = invocation_context.pair_index;
    update_event.camera_timestamp_ns = invocation_context.camera_timestamp_ns;
    update_event.invocation_id = invocation_id;
  }
  if (!cp2_size_to_u64(feature_vec.size(), update_event.input_feature_count)) {
    if (recorded_mode) {
      latch_cp2_trace_fatal(CP2TraceFatalReason::kRequiredEncoding,
                            "CP2 input feature count exceeds u64");
    }
    throw std::overflow_error("UpdaterMSCKF input feature count exceeds u64");
  }

  const auto notify_observer = [&update_callback](const CP2LiveUpdateEvent &event,
                                                   bool baseline_committed) {
    if (!update_callback || !*update_callback) {
      return;
    }
    try {
      (*update_callback)(event);
    } catch (const std::exception &exception) {
      PRINT_WARNING(YELLOW
                    "[MSCKF-CP2-OBSERVER]: status=callback_exception detail=%s "
                    "baseline_committed=%d\n" RESET,
                    exception.what(), baseline_committed ? 1 : 0);
    } catch (...) {
      PRINT_WARNING(YELLOW
                    "[MSCKF-CP2-OBSERVER]: status=callback_exception detail=unknown "
                    "baseline_committed=%d\n" RESET,
                    baseline_committed ? 1 : 0);
    }
  };

  bool record_published = false;
  const auto publish_record = [this, &recorded_sink, &recorded_event,
                               &record_published, &notify_observer]() {
    if (!recorded_sink || !recorded_event || record_published) {
      latch_cp2_trace_fatal(CP2TraceFatalReason::kConfigurationInvariant,
                            "CP2 authoritative publication state is invalid");
    }
    const std::shared_ptr<const CP2RecordedUpdateEvent> immutable_record =
        recorded_event;
    const CP2RecordedSinkStatus sink_status =
        recorded_sink->Publish(immutable_record);
    if (sink_status != CP2RecordedSinkStatus::kPublished) {
      latch_cp2_trace_fatal(CP2TraceFatalReason::kSinkRejected,
                            "CP2 authoritative sink rejected the update record");
    }
    record_published = true;
    notify_observer(immutable_record->update,
                    immutable_record->update.baseline_commit_occurred);
  };

  std::size_t ordinary_mean_commit_count = 0;
  std::size_t ordinary_covariance_commit_count = 0;
  std::size_t ordinary_feature_finalization_count = 0;
  int ordinary_attempted_passes = 0;
  int ordinary_completed_passes = 0;
  int ordinary_selected_pass = 0;
  bool ordinary_iteration_terminal_logged = false;

  const auto finish_update = [this, &notify_observer, &update_event,
                              &cp2_update_start, &state, &recorded_mode,
                              &ordinary_mean_commit_count,
                              &ordinary_covariance_commit_count,
                              &ordinary_feature_finalization_count,
                              &ordinary_attempted_passes,
                              &ordinary_completed_passes,
                              &ordinary_selected_pass,
                              &ordinary_iteration_terminal_logged](CP2UpdateTerminalStatus status,
                                                 CP2UpdateTerminalSubreason terminal_subreason) {
    const CP2SteadyClockEndpoint cp2_update_end =
        cp2_steady_clock_now();
#if defined(OV_MSCKF_CP2_TESTING)
    cp2_test_note_timing_end();
#endif
    const bool duration_valid =
        capture_cp2_duration_ns(cp2_update_start, cp2_update_end,
                                update_event);
    update_event.terminal_status = duration_valid ? status : CP2UpdateTerminalStatus::kInternalFailure;
    update_event.terminal_subreason =
        duration_valid ? terminal_subreason : CP2UpdateTerminalSubreason::kTraceInvariantFailure;
    if (!recorded_mode && !ordinary_iteration_terminal_logged) {
      PRINT_DEBUG(
          "[MSCKF-ITER]: timestamp=%.17g terminal=1 requested_passes=%d "
          "attempted_passes=%d completed_passes=%d selected_pass=%d "
          "status=%s reason=%s mean_commits=%zu covariance_commits=%zu "
          "feature_finalizations=%zu\n",
          state ? state->_timestamp : 0.0, _options.max_visual_passes,
          ordinary_attempted_passes, ordinary_completed_passes,
          ordinary_selected_pass,
          cp2_update_terminal_status_name(update_event.terminal_status),
          cp2_update_terminal_subreason_name(update_event.terminal_subreason),
          ordinary_mean_commit_count, ordinary_covariance_commit_count,
          ordinary_feature_finalization_count);
      ordinary_iteration_terminal_logged = true;
    }
    notify_observer(update_event, false);
  };

  const auto finish_recorded_zero_raw =
      [this, &update_event, &recorded_event, &cp2_update_start,
       &publish_record](CP2UpdateTerminalStatus status,
                        CP2UpdateTerminalSubreason terminal_subreason) {
        const CP2SteadyClockEndpoint cp2_update_end =
            cp2_steady_clock_now();
#if defined(OV_MSCKF_CP2_TESTING)
        cp2_test_note_timing_end();
#endif
        update_event.terminal_status = status;
        update_event.terminal_subreason = terminal_subreason;
        const bool duration_valid =
#if defined(OV_MSCKF_CP2_TESTING)
            cp2_test_fault !=
                CP2UpdaterTestFault::kNoncommitDurationArithmeticFailure &&
#endif
            capture_cp2_duration_ns(cp2_update_start, cp2_update_end,
                                    update_event);
        if (!duration_valid) {
          latch_cp2_trace_fatal(CP2TraceFatalReason::kArithmeticInvariant,
                                "CP2 noncommit duration is not representable");
        }
        if (update_event.raw_system_count != 0U || !recorded_event) {
          latch_cp2_trace_fatal(CP2TraceFatalReason::kConfigurationInvariant,
                                "CP2 zero-raw finalizer received a raw system");
        }
        recorded_event->state_phase_count = 0U;
        recorded_event->online_math_evidence_passed =
            status != CP2UpdateTerminalStatus::kInternalFailure;
        recorded_event->update = std::move(update_event);
        publish_record();
  };
  bool live_commit_started = false;

  try {

  // Return if no features
  if (feature_vec.empty()) {
    if (recorded_mode) {
      finish_recorded_zero_raw(CP2UpdateTerminalStatus::kEmptyInput,
                               CP2UpdateTerminalSubreason::kInputEmpty);
    } else {
      finish_update(CP2UpdateTerminalStatus::kEmptyInput,
                    CP2UpdateTerminalSubreason::kInputEmpty);
    }
    return;
  }

  // Start timing
  boost::posix_time::ptime rT0, rT1, rT2, rT3, rT4, rT5;
  rT0 = boost::posix_time::microsec_clock::local_time();

  // Ordinary mode freezes the complete predicted prior before any feature
  // cleanup or geometry write, and performs all feature work on detached
  // copies. Recorded mode retains its existing live-feature lifecycle.
  MSCKFUpdatePriorSnapshot ordinary_prior;
  StateOptions ordinary_state_options;
  std::unique_ptr<MSCKFDetachedFeatureBatch> ordinary_feature_batch;
  std::vector<std::shared_ptr<Feature>> *proposal_features = &feature_vec;
  if (!recorded_mode) {
    ordinary_state_options = state->_options;
    ordinary_prior = UpdaterMSCKFPreview::CapturePrior(state);
    const MSCKFUpdatePriorMatchResult entry_match =
        UpdaterMSCKFPreview::MatchPrior(state, ordinary_prior);
    if (!entry_match.accepted()) {
      PRINT_WARNING(YELLOW
                    "[MSCKF-PRIOR]: status=%s stage=entry index=%d live_writes=0\n" RESET,
                    msckf_update_prior_match_status_name(entry_match.status),
                    (int)entry_match.offending_index);
      // The ordinary caller interprets every returned pointer as a consumed
      // MSCKF track. An empty output is therefore the rollback signal that
      // preserves the untouched database objects for a later update.
      feature_vec.clear();
      PRINT_WARNING(
          YELLOW
          "[MSCKF-ITER]: timestamp=%.17g terminal=1 requested_passes=%d "
          "attempted_passes=0 completed_passes=0 selected_pass=0 "
          "status=prior_mismatch reason=snapshot mean_commits=0 "
          "covariance_commits=0 feature_finalizations=0\n"
          RESET,
          ordinary_prior.timestamp, _options.max_visual_passes);
      ordinary_iteration_terminal_logged = true;
      finish_update(CP2UpdateTerminalStatus::kInternalFailure,
                    CP2UpdateTerminalSubreason::kSnapshotMismatch);
      return;
    }
    if (_options.max_visual_passes == 2) {
      bool visual_contract_supported =
          _options.landmark_elimination ==
              UpdaterOptions::LandmarkElimination::SCHUR &&
          ordinary_prior.do_fej &&
          ordinary_prior.feature_representation ==
              static_cast<int>(
                  LandmarkRepresentation::Representation::GLOBAL_3D) &&
          !ordinary_prior.cameras.empty();
      for (const MSCKFUpdatePriorCamera &camera : ordinary_prior.cameras) {
        visual_contract_supported =
            visual_contract_supported &&
            camera.model == MSCKFUpdatePriorCameraModel::kRadtan;
      }
      if (!visual_contract_supported) {
        PRINT_WARNING(
            YELLOW
            "[MSCKF-ITER]: timestamp=%.17g terminal=1 requested_passes=2 "
            "attempted_passes=0 completed_passes=0 selected_pass=0 "
            "status=unsupported_contract reason=unsupported_contract "
            "requires=schur,fej_on,global_3d,camera_radtan "
            "mean_commits=0 covariance_commits=0 feature_finalizations=0 "
            "live_writes=0\n"
            RESET,
            ordinary_prior.timestamp);
        ordinary_iteration_terminal_logged = true;
        feature_vec.clear();
        finish_update(CP2UpdateTerminalStatus::kInternalFailure,
                      CP2UpdateTerminalSubreason::kInvalidLiveMode);
        return;
      }
    }
    ordinary_feature_batch.reset(
        new MSCKFDetachedFeatureBatch(feature_vec));
    proposal_features = &ordinary_feature_batch->active();
  }
  std::vector<std::shared_ptr<Feature>> &proposal_feature_vec =
      *proposal_features;
  ordinary_attempted_passes = 1;
  const auto finalize_ordinary_features = [&]() {
    if (ordinary_feature_batch) {
      if (ordinary_feature_finalization_count != 0) {
        throw std::logic_error(
            "UpdaterMSCKF ordinary feature finalization attempted twice");
      }
      ordinary_feature_batch->PrepareFinalization();
      ordinary_feature_batch->FinalizePrepared(feature_vec);
      ++ordinary_feature_finalization_count;
    }
  };
  const auto commit_ordinary_update =
      [&](const Eigen::VectorXd &dx, const Eigen::MatrixXd &covariance,
          StateHelper::PrecomputedCovariancePolicy covariance_policy) {
        if (!ordinary_feature_batch || ordinary_mean_commit_count != 0 ||
            ordinary_covariance_commit_count != 0) {
          return false;
        }
        if (!StateHelper::CommitPrecomputedUpdate(
                state, dx, covariance, covariance_policy)) {
          return false;
        }
        ++ordinary_mean_commit_count;
        ++ordinary_covariance_commit_count;
        return true;
      };

  // 0. Get all timestamps our clones are at (and thus valid measurement times)
  std::vector<double> clonetimes;
  for (const auto &clone_imu : state->_clones_IMU) {
    clonetimes.emplace_back(clone_imu.first);
  }

  // 1. Clean all feature measurements and make sure they all have valid clone times
  auto it0 = proposal_feature_vec.begin();
  while (it0 != proposal_feature_vec.end()) {

    // Clean the feature
    (*it0)->clean_old_measurements(clonetimes);

    // Count how many measurements
    std::uint64_t feature_measurement_count = 0U;
    for (const auto &pair : (*it0)->timestamps) {
      std::uint64_t camera_measurement_count = 0U;
      std::uint64_t updated_measurement_count = 0U;
      if (!cp2_size_to_u64((*it0)->timestamps[pair.first].size(),
                           camera_measurement_count) ||
          !cp2_checked_add_u64(feature_measurement_count,
                               camera_measurement_count,
                               updated_measurement_count)) {
        if (recorded_mode) {
          finish_recorded_zero_raw(
              CP2UpdateTerminalStatus::kInternalFailure,
              CP2UpdateTerminalSubreason::kTraceInvariantFailure);
          return;
        }
        throw std::overflow_error("UpdaterMSCKF feature measurement count overflows u64");
      }
      feature_measurement_count = updated_measurement_count;
    }

    // Remove if we don't have enough
    if (feature_measurement_count < 2U) {
      (*it0)->to_delete = true;
      it0 = proposal_feature_vec.erase(it0);
    } else {
      it0++;
    }
  }
  rT1 = boost::posix_time::microsec_clock::local_time();
  const bool features_after_cleaning = !proposal_feature_vec.empty();

  // 2. Create vector of cloned *CAMERA* poses at each of our clone timesteps
  std::unordered_map<size_t, std::unordered_map<double, FeatureInitializer::ClonePose>> clones_cam;
  for (const auto &clone_calib : state->_calib_IMUtoCAM) {

    // For this camera, create the vector of camera poses
    std::unordered_map<double, FeatureInitializer::ClonePose> clones_cami;
    for (const auto &clone_imu : state->_clones_IMU) {

      // Get current camera pose
      Eigen::Matrix<double, 3, 3> R_GtoCi = clone_calib.second->Rot() * clone_imu.second->Rot();
      Eigen::Matrix<double, 3, 1> p_CioinG = clone_imu.second->pos() - R_GtoCi.transpose() * clone_calib.second->pos();

      // Append to our map
      clones_cami.insert({clone_imu.first, FeatureInitializer::ClonePose(R_GtoCi, p_CioinG)});
    }

    // Append to our map
    clones_cam.insert({clone_calib.first, clones_cami});
  }

  // 3. Try to triangulate all MSCKF or new SLAM features that have measurements
  auto it1 = proposal_feature_vec.begin();
  while (it1 != proposal_feature_vec.end()) {

    // Triangulate the feature and remove if it fails
    bool success_tri = true;
    if (initializer_feat->config().triangulate_1d) {
      success_tri = initializer_feat->single_triangulation_1d(*it1, clones_cam);
    } else {
      success_tri = initializer_feat->single_triangulation(*it1, clones_cam);
    }

    // Gauss-newton refine the feature
    bool success_refine = true;
    if (initializer_feat->config().refine_features) {
      success_refine = initializer_feat->single_gaussnewton(*it1, clones_cam);
    }

    // Remove the feature if not a success
    if (!success_tri || !success_refine) {
      (*it1)->to_delete = true;
      it1 = proposal_feature_vec.erase(it1);
      continue;
    }
    it1++;
  }
  rT2 = boost::posix_time::microsec_clock::local_time();
  const bool features_after_triangulation = !proposal_feature_vec.empty();

  // Calculate the max possible measurement size with checked schema arithmetic.
  std::uint64_t max_meas_size_u64 = 0U;
  for (std::size_t i = 0; i < proposal_feature_vec.size(); ++i) {
    for (const auto &pair : proposal_feature_vec.at(i)->timestamps) {
      std::uint64_t observation_count = 0U;
      std::uint64_t scalar_row_count = 0U;
      std::uint64_t updated_maximum = 0U;
      if (!cp2_size_to_u64(proposal_feature_vec.at(i)->timestamps[pair.first].size(),
                           observation_count) ||
          !cp2_checked_multiply_u64(2U, observation_count, scalar_row_count) ||
          !cp2_checked_add_u64(max_meas_size_u64, scalar_row_count,
                               updated_maximum)) {
        if (recorded_mode) {
          finish_recorded_zero_raw(
              CP2UpdateTerminalStatus::kInternalFailure,
              CP2UpdateTerminalSubreason::kTraceInvariantFailure);
          return;
        }
        throw std::overflow_error("UpdaterMSCKF measurement capacity overflows u64");
      }
      max_meas_size_u64 = updated_maximum;
    }
  }
  if (max_meas_size_u64 >
      static_cast<std::uint64_t>(std::numeric_limits<Eigen::Index>::max())) {
    if (recorded_mode) {
      finish_recorded_zero_raw(
          CP2UpdateTerminalStatus::kInternalFailure,
          CP2UpdateTerminalSubreason::kTraceInvariantFailure);
      return;
    }
    throw std::overflow_error("UpdaterMSCKF measurement capacity exceeds Eigen::Index");
  }
  const Eigen::Index max_meas_size =
      static_cast<Eigen::Index>(max_meas_size_u64);

  // Calculate max possible state size (i.e. the size of our covariance)
  // NOTE: that when we have the single inverse depth representations, those are only 1dof in size
  Eigen::Index max_hx_size = state->max_covariance_size();
  if (max_hx_size < 0) {
    if (recorded_mode) {
      finish_recorded_zero_raw(
          CP2UpdateTerminalStatus::kInternalFailure,
          CP2UpdateTerminalSubreason::kTraceInvariantFailure);
      return;
    }
    throw std::overflow_error("UpdaterMSCKF covariance size is negative");
  }
  for (auto &landmark : state->_features_SLAM) {
    if (!landmark.second || landmark.second->size() < 0 ||
        landmark.second->size() > max_hx_size) {
      if (recorded_mode) {
        finish_recorded_zero_raw(
            CP2UpdateTerminalStatus::kInternalFailure,
            CP2UpdateTerminalSubreason::kTraceInvariantFailure);
        return;
      }
      throw std::overflow_error("UpdaterMSCKF SLAM exclusion size is invalid");
    }
    max_hx_size -= landmark.second->size();
  }

  // Large Jacobian and residual of *all* features for this update
  Eigen::VectorXd res_big = Eigen::VectorXd::Zero(max_meas_size);
  Eigen::MatrixXd Hx_big = Eigen::MatrixXd::Zero(max_meas_size, max_hx_size);
  std::unordered_map<std::shared_ptr<Type>, Eigen::Index> Hx_mapping;
  std::vector<std::shared_ptr<Type>> Hx_order_big;
  Eigen::Index ct_jacob = 0;
  Eigen::Index ct_meas = 0;
  double retained_gamma = 0.0;
  bool retained_gamma_available = true;
  double two_pass_one_max_feature_nis =
      std::numeric_limits<double>::quiet_NaN();
  double two_pass_one_max_gate_threshold =
      std::numeric_limits<double>::quiet_NaN();

  // Recorded mode takes exactly one tentative full composite at this existing
  // post-triangulation/pre-raw immutable-prior boundary. It is not validated or
  // emitted unless the first raw system becomes possible.
  CP2CompositeStateCapture phase0_capture;
  CP2CompositeStateStatus phase0_capture_status =
      CP2CompositeStateStatus::kAccepted;
  MSCKFUpdatePreviewSnapshot prior_snapshot;
  if (recorded_mode) {
#if defined(OV_MSCKF_CP2_TESTING)
    cp2_test_note_stage(CP2UpdaterTestStage::kPhase0Capture);
#endif
    phase0_capture_status = CP2CompositeStateAdapter::Capture(
        state, CP2StatePhase::kPhase0Prior, phase0_capture);
  } else {
#if defined(OV_MSCKF_CP2_TESTING)
    cp2_test_note_stage(CP2UpdaterTestStage::kLivePreviewCapture);
#endif
    prior_snapshot = ordinary_prior.filter;
  }
  bool phase0_promoted = false;
  std::vector<std::uint8_t> phase0_payload;
  const bool observer_enabled = static_cast<bool>(update_callback);
  const bool shadow_enabled =
      recorded_mode ||
      (observer_enabled && shadow_requested &&
       _options.landmark_elimination == UpdaterOptions::LandmarkElimination::NULLSPACE);
  CP2ShadowMathInput shadow_input;
  if (shadow_enabled) {
    if (!recorded_mode) {
      shadow_input.prior = prior_snapshot;
    }
    shadow_input.sigma_px = _options.sigma_pix;
    shadow_input.sigma_px_sq = _options.sigma_pix_sq;
    shadow_input.chi2_multiplier = _options.chi2_multipler;
    shadow_input.chi_squared_table = chi_squared_table;
    shadow_input.raw_systems.reserve(feature_vec.size());
  }

  bool recorded_shadow_trace_valid = !recorded_mode;
  bool recorded_shadow_math_valid = !recorded_mode;
  bool recorded_baseline_provenance_equal = !recorded_mode;
  bool shadow_baseline_proposal_payload_available = false;
  std::vector<std::uint8_t> shadow_baseline_proposal_payload;

  const auto capture_recorded_phase1 =
      [this, &state, &phase0_capture, &phase0_payload](
          CP2CompositeStateCapture &phase1_capture,
          std::vector<std::uint8_t> &phase1_payload,
          bool &canonical_equal) {
#if defined(OV_MSCKF_CP2_TESTING)
        cp2_test_note_stage(CP2UpdaterTestStage::kPhase1Capture);
#endif
        const CP2CompositeStateStatus capture_status =
            CP2CompositeStateAdapter::Capture(
                state, CP2StatePhase::kPhase1Precommit, phase1_capture);
        if (capture_status != CP2CompositeStateStatus::kAccepted) {
          latch_cp2_trace_fatal(CP2TraceFatalReason::kPhase1Capture,
                                "CP2 phase 1 capture is incomplete");
        }
        try {
          phase1_payload = CP2StateTraceCodec::EncodeSnapshotPayload(
              phase1_capture.snapshot);
        } catch (...) {
          latch_cp2_trace_fatal(CP2TraceFatalReason::kRequiredEncoding,
                                "CP2 phase 1 payload encoding failed");
        }

        // Value bytes are classified before any independent phase-1 validity
        // judgment. Equality with the already valid phase 0 proves validity.
        const bool payload_equal = phase0_payload == phase1_payload;
        const bool value_equal = CP2CompositeStateAdapter::CanonicallyEqual(
            phase0_capture.snapshot, phase1_capture.snapshot);
        canonical_equal = payload_equal && value_equal;
      };

  const auto install_recorded_phase01 =
      [this, &recorded_event, &phase0_capture, &phase0_payload](
          CP2CompositeStateCapture &phase1_capture,
          std::vector<std::uint8_t> &phase1_payload,
          bool canonical_equal, bool pointer_equal) {
        if (!recorded_event || recorded_event->state_phase_count != 0U ||
            recorded_event->state_phases[0].snapshot ||
            recorded_event->state_phases[1].snapshot) {
          latch_cp2_trace_fatal(CP2TraceFatalReason::kConfigurationInvariant,
                                "CP2 phase 0/1 owning slots are not empty");
        }
        try {
          recorded_event->state_phases[0].phase =
              CP2StatePhase::kPhase0Prior;
          recorded_event->state_phases[0].snapshot.reset(
              new CP2CompositeStateSnapshot(std::move(phase0_capture.snapshot)));
          recorded_event->state_phases[0].payload = std::move(phase0_payload);
          recorded_event->state_phases[1].phase =
              CP2StatePhase::kPhase1Precommit;
          recorded_event->state_phases[1].snapshot.reset(
              new CP2CompositeStateSnapshot(std::move(phase1_capture.snapshot)));
          recorded_event->state_phases[1].payload = std::move(phase1_payload);
        } catch (...) {
          latch_cp2_trace_fatal(CP2TraceFatalReason::kRequiredEncoding,
                                "CP2 could not own the complete phase 0/1 pair");
        }
        recorded_event->state_phase_count = 2U;
        recorded_event->phase01_canonical_equal = canonical_equal;
        recorded_event->phase01_pointer_graph_equal = pointer_equal;
      };

  const auto publish_installed_recorded_noncommit =
      [this, &update_event, &recorded_event, &cp2_update_start,
       &recorded_shadow_trace_valid, &recorded_baseline_provenance_equal,
       &recorded_shadow_math_valid,
       &publish_record](CP2UpdateTerminalStatus status,
                        CP2UpdateTerminalSubreason subreason) {
        if (!recorded_event || recorded_event->state_phase_count != 2U ||
            !recorded_event->state_phases[0].snapshot ||
            !recorded_event->state_phases[1].snapshot) {
          latch_cp2_trace_fatal(CP2TraceFatalReason::kRequiredEncoding,
                                "CP2 noncommit phase population is incomplete");
        }
        if (!recorded_event->phase01_canonical_equal ||
            !recorded_event->phase01_pointer_graph_equal) {
          status = CP2UpdateTerminalStatus::kInternalFailure;
          subreason = CP2UpdateTerminalSubreason::kSnapshotMismatch;
        } else if (!recorded_shadow_trace_valid ||
                   !recorded_baseline_provenance_equal) {
          status = CP2UpdateTerminalStatus::kInternalFailure;
          subreason = CP2UpdateTerminalSubreason::kTraceInvariantFailure;
        }
        update_event.terminal_status = status;
        update_event.terminal_subreason = subreason;
        recorded_event->online_math_evidence_passed =
            status != CP2UpdateTerminalStatus::kInternalFailure &&
            update_event.baseline_gamma_status != CP2GammaStatus::kNonfinite &&
            recorded_shadow_trace_valid && recorded_shadow_math_valid &&
            recorded_baseline_provenance_equal;

        // The endpoint is the last timed estimator operation for a noncommit.
        const CP2SteadyClockEndpoint cp2_update_end =
            cp2_steady_clock_now();
#if defined(OV_MSCKF_CP2_TESTING)
        cp2_test_note_timing_end();
#endif
        const bool duration_valid =
#if defined(OV_MSCKF_CP2_TESTING)
            cp2_test_fault !=
                CP2UpdaterTestFault::kNoncommitDurationArithmeticFailure &&
#endif
            capture_cp2_duration_ns(cp2_update_start, cp2_update_end,
                                    update_event);
        if (!duration_valid) {
          latch_cp2_trace_fatal(CP2TraceFatalReason::kArithmeticInvariant,
                                "CP2 noncommit duration is not representable");
        }
        recorded_event->update = std::move(update_event);
        publish_record();
      };

  const auto finish_recorded_noncommit =
      [this, &state, &phase0_capture, &capture_recorded_phase1,
       &install_recorded_phase01, &publish_installed_recorded_noncommit,
       &update_event](CP2UpdateTerminalStatus status,
                      CP2UpdateTerminalSubreason subreason) {
        if (update_event.raw_system_count == 0U) {
          latch_cp2_trace_fatal(CP2TraceFatalReason::kConfigurationInvariant,
                                "CP2 phase-pair finalizer requires raw evidence");
        }
        CP2CompositeStateCapture phase1_capture;
        std::vector<std::uint8_t> phase1_payload;
        bool canonical_equal = false;
        capture_recorded_phase1(phase1_capture, phase1_payload,
                                canonical_equal);
        const bool pointer_equal = CP2CompositeStateAdapter::PointerGraphMatches(
            state, phase0_capture.pointer_graph);
        install_recorded_phase01(phase1_capture, phase1_payload,
                                 canonical_equal, pointer_equal);
        publish_installed_recorded_noncommit(status, subreason);
      };

  // 4. Compute linear system for each feature, nullspace project, and reject
  auto it2 = proposal_feature_vec.begin();
  while (it2 != proposal_feature_vec.end()) {

    // Convert our feature into our current format
    UpdaterHelper::UpdaterHelperFeature feat;
    feat.featid = (*it2)->featid;
    feat.uvs = (*it2)->uvs;
    feat.uvs_norm = (*it2)->uvs_norm;
    feat.timestamps = (*it2)->timestamps;

    // If we are using single inverse depth, then it is equivalent to using the msckf inverse depth
    feat.feat_representation = state->_options.feat_rep_msckf;
    if (state->_options.feat_rep_msckf == LandmarkRepresentation::Representation::ANCHORED_INVERSE_DEPTH_SINGLE) {
      feat.feat_representation = LandmarkRepresentation::Representation::ANCHORED_MSCKF_INVERSE_DEPTH;
    }

    // Save the position and its fej value
    if (LandmarkRepresentation::is_relative_representation(feat.feat_representation)) {
      feat.anchor_cam_id = (*it2)->anchor_cam_id;
      feat.anchor_clone_timestamp = (*it2)->anchor_clone_timestamp;
      feat.p_FinA = (*it2)->p_FinA;
      feat.p_FinA_fej = (*it2)->p_FinA;
    } else {
      feat.p_FinG = (*it2)->p_FinG;
      feat.p_FinG_fej = (*it2)->p_FinG;
    }

    // Our return values (feature jacobian, state jacobian, residual, and order of state jacobian)
    Eigen::MatrixXd H_f;
    Eigen::MatrixXd H_x;
    Eigen::VectorXd res;
    std::vector<std::shared_ptr<Type>> Hx_order;

    if (recorded_mode && !phase0_promoted) {
      if (phase0_capture_status != CP2CompositeStateStatus::kAccepted) {
        latch_cp2_trace_fatal(CP2TraceFatalReason::kPhase0Promotion,
                              "CP2 tentative phase 0 capture is incomplete");
      }
      try {
        // Encoding precedes validation by contract. These exact bytes, rather
        // than a later recapture, become the promoted phase-0 payload.
        phase0_payload = CP2StateTraceCodec::EncodeSnapshotPayload(
            phase0_capture.snapshot);
      } catch (...) {
        latch_cp2_trace_fatal(CP2TraceFatalReason::kPhase0Promotion,
                              "CP2 tentative phase 0 cannot be encoded");
      }
      const CP2CompositeStateStatus phase0_validation_status =
          CP2CompositeStateAdapter::Validate(phase0_capture.snapshot);
      CP2CompositeStateStatus phase0_projection_status =
          CP2CompositeStateStatus::kNotPrepared;
      if (phase0_validation_status == CP2CompositeStateStatus::kAccepted) {
#if defined(OV_MSCKF_CP2_TESTING)
        cp2_test_note_stage(CP2UpdaterTestStage::kPhase0Projection);
#endif
        phase0_projection_status = CP2CompositeStateAdapter::ProjectPreview(
            phase0_capture.snapshot, prior_snapshot);
      }
      if (phase0_validation_status != CP2CompositeStateStatus::kAccepted ||
          phase0_projection_status != CP2CompositeStateStatus::kAccepted) {
        latch_cp2_trace_fatal(CP2TraceFatalReason::kPhase0Promotion,
                              "CP2 tentative phase 0 failed validation or projection");
      }
      shadow_input.prior = prior_snapshot;
      phase0_promoted = true;
    }

    // Get the Jacobian for this feature
#if defined(OV_MSCKF_CP2_TESTING)
    if (cp2_test_raw_assembly_calls ==
        std::numeric_limits<std::uint64_t>::max()) {
      throw std::overflow_error("CP2 test raw-assembly counter overflows u64");
    }
    ++cp2_test_raw_assembly_calls;
#endif
    UpdaterHelper::get_feature_jacobian_full(state, feat, H_f, H_x, res, Hx_order);
    std::uint64_t incremented_raw_count = 0U;
    if (!cp2_checked_add_u64(update_event.raw_system_count, 1U,
                             incremented_raw_count)) {
      throw std::overflow_error("UpdaterMSCKF raw-system counter overflows u64");
    }
    update_event.raw_system_count = incremented_raw_count;

    // This is the sole shared raw seam: owning copies and value-only layout
    // are captured immediately after production assembly and before either
    // reducer can mutate H_f, H_x, or res.
    const std::vector<CP2FeatureGateLayoutBlock> feature_layout = capture_cp2_feature_layout(Hx_order);
    if (!recorded_mode && conditioning_capture_writer) {
      conditioning_capture_writer->TryCapture(state, feat, ordinary_prior,
                                               Hx_order, H_x, H_f, res);
    }
    if (shadow_enabled) {
      CP2RawFeatureSystem raw;
      if (!cp2_size_to_u64(feat.featid, raw.feature_id)) {
        throw std::overflow_error("UpdaterMSCKF feature ID exceeds u64");
      }
      raw.H_x = H_x;
      raw.H_f = H_f;
      raw.residual = res;
      raw.jacobian_layout = feature_layout;
      if (recorded_mode) {
        try {
          recorded_event->raw_system_payloads.push_back(
              CP2TraceCodec::EncodeRawSystemPayload(raw));
        } catch (...) {
          latch_cp2_trace_fatal(CP2TraceFatalReason::kRequiredEncoding,
                                "CP2 raw-system payload encoding failed");
        }
      }
      shadow_input.raw_systems.push_back(std::move(raw));
    }

    // Eliminate the transient landmark. The baseline keeps its original
    // Givens nullspace path; the candidate emits an equivalent square-root
    // Schur row system after the signed rank/conditioning checks.
    double feature_gamma = 0.0;
    bool reduction_evidence_available = true;
    if (_options.landmark_elimination == UpdaterOptions::LandmarkElimination::SCHUR) {
      SchurReductionResult reduction = SchurUpdate::Reduce(H_x, H_f, res, _options.sigma_pix);
      if (!reduction.accepted()) {
        if (!reduction.singular_values_available) {
          PRINT_WARNING(YELLOW
                        "[MSCKF-SCHUR]: feature=%zu pass=1 rows=%d status=%s stage=%s singular_values=unavailable "
                        "singular_ratio=unavailable jitter=%zu clamp=%zu regularization=%zu fallback=%zu\n" RESET,
                        feat.featid, (int)reduction.raw_rows, schur_reduction_status_name(reduction.status),
                        schur_reduction_stage_name(reduction.stage), reduction.jitter_count, reduction.clamp_count,
                        reduction.regularization_count, reduction.fallback_count);
        } else if (!reduction.singular_ratio_available) {
          PRINT_WARNING(YELLOW
                        "[MSCKF-SCHUR]: feature=%zu pass=1 rows=%d status=%s stage=%s singular_values=[%.17g,%.17g,%.17g] "
                        "singular_ratio=unavailable jitter=%zu clamp=%zu regularization=%zu fallback=%zu\n" RESET,
                        feat.featid, (int)reduction.raw_rows, schur_reduction_status_name(reduction.status),
                        schur_reduction_stage_name(reduction.stage), reduction.singular_values(0), reduction.singular_values(1),
                        reduction.singular_values(2), reduction.jitter_count, reduction.clamp_count,
                        reduction.regularization_count, reduction.fallback_count);
        } else {
          PRINT_WARNING(YELLOW
                        "[MSCKF-SCHUR]: feature=%zu pass=1 rows=%d status=%s stage=%s singular_values=[%.17g,%.17g,%.17g] "
                        "singular_ratio=%.17g jitter=%zu clamp=%zu regularization=%zu fallback=%zu\n" RESET,
                        feat.featid, (int)reduction.raw_rows, schur_reduction_status_name(reduction.status),
                        schur_reduction_stage_name(reduction.stage), reduction.singular_values(0), reduction.singular_values(1),
                        reduction.singular_values(2), reduction.singular_ratio, reduction.jitter_count, reduction.clamp_count,
                        reduction.regularization_count, reduction.fallback_count);
        }
        (*it2)->to_delete = true;
        it2 = proposal_feature_vec.erase(it2);
        continue;
      }
      H_x = std::move(reduction.H_reduced);
      res = std::move(reduction.residual_reduced);
      feature_gamma = reduction.gamma;
    } else if (_options.landmark_elimination == UpdaterOptions::LandmarkElimination::NULLSPACE) {
      UpdaterHelper::nullspace_project_inplace(H_f, H_x, res);
      const Eigen::VectorXd whitened_residual = res.array() / _options.sigma_pix;
      feature_gamma = whitened_residual.squaredNorm();
      reduction_evidence_available = H_x.allFinite() && res.allFinite() && whitened_residual.allFinite() &&
                                     std::isfinite(feature_gamma);
      if (!reduction_evidence_available) {
        PRINT_WARNING(YELLOW
                      "[MSCKF-REDUCTION]: feature=%zu pass=1 rows=%d status=nonfinite stage=statistics "
                      "jitter=0 clamp=0 regularization=0 fallback=0\n" RESET,
                      feat.featid, (int)res.rows());
      }
    } else {
      PRINT_ERROR(RED "[MSCKF-REDUCTION]: feature=%zu pass=1 rows=%d status=invalid_mode fallback=0\n" RESET,
                  feat.featid, (int)H_f.rows());
      if (recorded_mode) {
        finish_recorded_noncommit(CP2UpdateTerminalStatus::kInternalFailure,
                                  CP2UpdateTerminalSubreason::kInvalidLiveMode);
      } else {
        feature_vec.clear();
        finish_update(CP2UpdateTerminalStatus::kInternalFailure,
                      CP2UpdateTerminalSubreason::kInvalidLiveMode);
      }
      return;
    }

    // The live lifecycle and the shadow both use this single frozen gate
    // implementation. An unavailable nullspace gamma is evidence-only: the
    // emitted production rows still execute their unchanged IEEE comparison.
    CP2FeatureGateInput gate_input =
        reduction_evidence_available
            ? CP2FeatureGateInput(H_x, res, prior_snapshot.covariance, feature_layout,
                                  _options.sigma_pix_sq, _options.chi2_multipler)
            : CP2FeatureGateInput::EmittedEvidenceUnavailable(
                  H_x, res, prior_snapshot.covariance, feature_layout, _options.sigma_pix_sq,
                  _options.chi2_multipler);
    const CP2FeatureGateResult gate = CP2FeatureGate::Evaluate(std::move(gate_input), chi_squared_table);
    if (!gate.lifecycle_accept) {
      PRINT_WARNING(YELLOW "[MSCKF-GATE]: feature=%zu pass=1 rows=%d status=rejected stage=%s\n" RESET,
                    feat.featid, (int)res.rows(), cp2_feature_gate_stage_name(gate.stage));
      (*it2)->to_delete = true;
      it2 = proposal_feature_vec.erase(it2);
      continue;
    }
    if (!recorded_mode && _options.max_visual_passes == 2 &&
        gate.chi2_available && gate.threshold_available &&
        std::isfinite(gate.chi2) && std::isfinite(gate.threshold)) {
      if (!std::isfinite(two_pass_one_max_feature_nis) ||
          gate.chi2 > two_pass_one_max_feature_nis) {
        two_pass_one_max_feature_nis = gate.chi2;
        two_pass_one_max_gate_threshold = gate.threshold;
      }
    }
    std::uint64_t accepted_feature_id = 0U;
    if (!cp2_size_to_u64(feat.featid, accepted_feature_id)) {
      throw std::overflow_error("UpdaterMSCKF accepted feature ID exceeds u64");
    }
    update_event.baseline_accepted_ids.push_back(accepted_feature_id);
    if (gate.stage == CP2FeatureGateStage::kThresholdNonfinite) {
      PRINT_WARNING(YELLOW
                    "[MSCKF-GATE]: feature=%zu pass=1 rows=%d status=evidence_unavailable stage=%s "
                    "lifecycle=accepted\n" RESET,
                    feat.featid, (int)res.rows(), cp2_feature_gate_stage_name(gate.stage));
    }

    // Gamma is objective evidence only. Evaluate each binary64 sum once and
    // record unavailability without erasing a feature or suppressing the live
    // compression, preflight, or commit.
    if (retained_gamma_available) {
      const double candidate_gamma = retained_gamma + feature_gamma;
      if (!std::isfinite(feature_gamma) || !std::isfinite(candidate_gamma)) {
        retained_gamma_available = false;
        retained_gamma = std::numeric_limits<double>::quiet_NaN();
      } else {
        retained_gamma = candidate_gamma;
      }
    }
    if (!retained_gamma_available) {
      PRINT_WARNING(YELLOW "[MSCKF-REDUCTION]: feature=%zu pass=1 rows=%d status=nonfinite reason=gamma_accumulation\n" RESET,
                    feat.featid, (int)res.rows());
    }

    if (H_x.rows() < 0 || H_x.cols() < 0 || res.rows() < 0 ||
        res.cols() != 1 || H_x.rows() != res.rows()) {
      throw std::overflow_error("UpdaterMSCKF accepted reduced-system shape is invalid");
    }
    Eigen::Index updated_measurement_rows = 0;
    if (!cp2_checked_add_eigen_index(ct_meas, res.rows(),
                                     updated_measurement_rows) ||
        updated_measurement_rows > max_meas_size) {
      throw std::overflow_error("UpdaterMSCKF measurement row count overflows");
    }

    // We are good!!! Append to our large H vector
    Eigen::Index ct_hx = 0;
    for (const auto &var : Hx_order) {

      if (!var || var->size() < 0) {
        throw std::overflow_error("UpdaterMSCKF Jacobian layout has invalid type size");
      }

      // Ensure that this variable is in our Jacobian
      if (Hx_mapping.find(var) == Hx_mapping.end()) {
        Eigen::Index updated_jacobian_columns = 0;
        if (!cp2_checked_add_eigen_index(ct_jacob, var->size(),
                                         updated_jacobian_columns) ||
            updated_jacobian_columns > max_hx_size) {
          throw std::overflow_error("UpdaterMSCKF Jacobian column count overflows");
        }
        Hx_mapping.insert({var, ct_jacob});
        Hx_order_big.push_back(var);
        ct_jacob = updated_jacobian_columns;
      }

      // Append to our large Jacobian
      Eigen::Index updated_feature_columns = 0;
      if (!cp2_checked_add_eigen_index(ct_hx, var->size(),
                                       updated_feature_columns) ||
          updated_feature_columns > H_x.cols()) {
        throw std::overflow_error("UpdaterMSCKF feature Jacobian columns overflow");
      }
      const auto mapped = Hx_mapping.find(var);
      if (mapped == Hx_mapping.end() || mapped->second < 0 ||
          var->size() > Hx_big.cols() - mapped->second) {
        throw std::overflow_error("UpdaterMSCKF global Jacobian mapping is invalid");
      }
      Hx_big.block(ct_meas, mapped->second, H_x.rows(), var->size()) =
          H_x.block(0, ct_hx, H_x.rows(), var->size());
      ct_hx = updated_feature_columns;
    }
    if (ct_hx != H_x.cols()) {
      throw std::overflow_error("UpdaterMSCKF feature Jacobian layout is incomplete");
    }

    // Append our residual and move forward
    res_big.block(ct_meas, 0, res.rows(), 1) = res;
    ct_meas = updated_measurement_rows;
    it2++;
  }
  rT3 = boost::posix_time::microsec_clock::local_time();
  if (update_event.raw_system_count > 0) {
    update_event.baseline_gamma_status =
        retained_gamma_available ? CP2GammaStatus::kAvailable : CP2GammaStatus::kNonfinite;
    update_event.baseline_gamma = retained_gamma;
  }

  // Run the pointer-free comparison only after all shared raw systems have
  // been captured. It owns an immutable pre-mutation prior and completes both
  // global proposals before the one possible baseline EKF commit below.
  if (shadow_enabled && !shadow_input.raw_systems.empty()) {
    update_event.shadow_evidence_available = true;
    update_event.shadow.input = std::move(shadow_input);
    try {
      update_event.shadow.result = CP2ShadowMath::Process(update_event.shadow.input);
#if defined(OV_MSCKF_CP2_TESTING)
      if (recorded_mode &&
          cp2_test_fault == CP2UpdaterTestFault::kCandidateAssemblyFailure) {
        update_event.shadow.result.schur_assembly_valid = false;
        update_event.shadow.result.input_valid = false;
        update_event.shadow.result.schur.proposal_available = false;
        update_event.shadow.result.schur.proposal = MSCKFUpdatePreviewResult();
      }
#endif
      update_event.shadow.shadow_math_completed = true;
      if (recorded_mode) {
        const CP2ShadowMathResult &shadow_result = update_event.shadow.result;
        recorded_shadow_trace_valid =
            shadow_result.traversal_complete &&
            shadow_result.features.size() ==
                update_event.shadow.input.raw_systems.size() &&
            recorded_event->raw_system_payloads.size() ==
                update_event.shadow.input.raw_systems.size();
        if (!recorded_shadow_trace_valid) {
          latch_cp2_trace_fatal(CP2TraceFatalReason::kShadowTrace,
                                "CP2 authoritative shadow traversal is incomplete");
        }
        recorded_shadow_math_valid =
            recorded_shadow_trace_valid &&
            cp2_shadow_math_evidence_passes(shadow_result);
        recorded_baseline_provenance_equal =
            recorded_shadow_trace_valid && shadow_result.raw_layouts_valid &&
            !shadow_result.duplicate_feature_id &&
            shadow_result.nullspace_assembly_valid &&
            shadow_result.nullspace.accepted_ids ==
                update_event.baseline_accepted_ids &&
            cp2_gamma_equal(shadow_result.nullspace.gamma_status,
                            shadow_result.nullspace.retained_gamma,
                            update_event.baseline_gamma_status,
                            update_event.baseline_gamma);

        if (shadow_result.nullspace.proposal_available) {
          try {
            shadow_baseline_proposal_payload =
                CP2TraceCodec::EncodeProposalPayload(cp2_proposal_payload(
                    shadow_result.nullspace.proposal));
            shadow_baseline_proposal_payload_available = true;
          } catch (...) {
            latch_cp2_trace_fatal(CP2TraceFatalReason::kRequiredEncoding,
                                  "CP2 baseline proposal encoding failed");
          }
        }
        if (shadow_result.schur.proposal_available) {
          try {
            recorded_event->candidate_proposal_payload =
                CP2TraceCodec::EncodeProposalPayload(
                    cp2_proposal_payload(shadow_result.schur.proposal));
            recorded_event->candidate_proposal_payload_available = true;
          } catch (...) {
            latch_cp2_trace_fatal(CP2TraceFatalReason::kRequiredEncoding,
                                  "CP2 candidate proposal encoding failed");
          }
        }
      }
    } catch (const CP2TraceFatalError &) {
      throw;
    } catch (const std::exception &exception) {
      if (recorded_mode) {
        latch_cp2_trace_fatal(CP2TraceFatalReason::kShadowTrace,
                              "CP2 authoritative shadow trace threw an exception");
      }
      PRINT_WARNING(YELLOW "[MSCKF-CP2-SHADOW]: status=exception detail=%s baseline_unchanged=1\n" RESET,
                    exception.what());
    } catch (...) {
      if (recorded_mode) {
        latch_cp2_trace_fatal(CP2TraceFatalReason::kShadowTrace,
                              "CP2 authoritative shadow trace threw an unknown exception");
      }
      PRINT_WARNING(YELLOW "[MSCKF-CP2-SHADOW]: status=exception detail=unknown baseline_unchanged=1\n" RESET);
    }
  }

  // We have appended all features to our Hx_big, res_big
  // Delete it so we do not reuse information
  for (size_t f = 0; f < proposal_feature_vec.size(); f++) {
    proposal_feature_vec[f]->to_delete = true;
  }

  if (update_event.raw_system_count > 0) {
    update_event.baseline_precompression_rows_available = true;
    if (!cp2_checked_eigen_index_to_u64(
            ct_meas, update_event.baseline_precompression_rows)) {
      if (recorded_mode) {
        finish_recorded_noncommit(
            CP2UpdateTerminalStatus::kInternalFailure,
            CP2UpdateTerminalSubreason::kTraceInvariantFailure);
        return;
      }
      throw std::overflow_error("UpdaterMSCKF precompression rows exceed u64");
    }
    update_event.baseline_precompression_system_nonempty = ct_meas > 0;
    if (recorded_mode) {
      recorded_baseline_provenance_equal =
          recorded_baseline_provenance_equal &&
          update_event.shadow.result.nullspace.precompression_rows == ct_meas;
    }
  }

  // Return if we don't have anything and resize our matrices
  if (ct_meas < 1) {
    if (recorded_mode && update_event.raw_system_count > 0U) {
      recorded_baseline_provenance_equal =
          recorded_baseline_provenance_equal &&
          update_event.shadow.result.nullspace.compressed_rows == 0 &&
          !update_event.shadow.result.nullspace.proposal_available &&
          !shadow_baseline_proposal_payload_available;
    }
    CP2UpdateTerminalSubreason terminal_subreason =
        CP2UpdateTerminalSubreason::kAllBaselineFeaturesRejected;
    if (!features_after_cleaning) {
      terminal_subreason = CP2UpdateTerminalSubreason::kNoFeaturesAfterCleaning;
    } else if (!features_after_triangulation) {
      terminal_subreason = CP2UpdateTerminalSubreason::kNoFeaturesAfterTriangulation;
    } else if (update_event.raw_system_count == 0) {
      terminal_subreason = CP2UpdateTerminalSubreason::kNoRawSystems;
    }
    if (recorded_mode) {
      if (update_event.raw_system_count == 0U) {
        finish_recorded_zero_raw(CP2UpdateTerminalStatus::kAllRejected,
                                 terminal_subreason);
      } else {
        finish_recorded_noncommit(CP2UpdateTerminalStatus::kAllRejected,
                                  terminal_subreason);
      }
    } else {
      finalize_ordinary_features();
      finish_update(CP2UpdateTerminalStatus::kAllRejected, terminal_subreason);
    }
    return;
  }
  assert(ct_meas <= max_meas_size);
  assert(ct_jacob <= max_hx_size);
  res_big.conservativeResize(ct_meas, 1);
  Hx_big.conservativeResize(ct_meas, ct_jacob);

  // 5. Perform measurement compression
  UpdaterHelper::measurement_compress_inplace(Hx_big, res_big);
  update_event.baseline_compressed_rows_available = true;
  if (!cp2_checked_eigen_index_to_u64(
          Hx_big.rows(), update_event.baseline_compressed_rows)) {
    if (recorded_mode) {
      finish_recorded_noncommit(
          CP2UpdateTerminalStatus::kInternalFailure,
          CP2UpdateTerminalSubreason::kTraceInvariantFailure);
      return;
    }
    throw std::overflow_error("UpdaterMSCKF compressed rows exceed u64");
  }
  if (Hx_big.rows() < 1) {
    if (recorded_mode) {
      recorded_baseline_provenance_equal =
          recorded_baseline_provenance_equal &&
          update_event.shadow.result.nullspace.compressed_rows == 0 &&
          !update_event.shadow.result.nullspace.proposal_available &&
          !shadow_baseline_proposal_payload_available;
    }
    if (recorded_mode) {
      finish_recorded_noncommit(
          CP2UpdateTerminalStatus::kEmptyAfterCompression,
          CP2UpdateTerminalSubreason::kMeasurementCompressionEmpty);
    } else {
      finalize_ordinary_features();
      finish_update(CP2UpdateTerminalStatus::kEmptyAfterCompression,
                    CP2UpdateTerminalSubreason::kMeasurementCompressionEmpty);
    }
    return;
  }
  update_event.baseline_compressed_system_nonempty = true;
  rT4 = boost::posix_time::microsec_clock::local_time();

  // Our noise is isotropic, so make it here after our compression
  Eigen::MatrixXd R_big = _options.sigma_pix_sq * Eigen::MatrixXd::Identity(res_big.rows(), res_big.rows());

  // The fixed two-pass estimator is an ordinary-mode, Schur-only branch. The
  // complete max=1/nullspace/recorded path below remains the established
  // one-pass implementation.
  if (!recorded_mode && _options.max_visual_passes == 2 &&
      _options.landmark_elimination ==
          UpdaterOptions::LandmarkElimination::SCHUR) {
    update_event.baseline_preflight_attempted = true;
    const auto reject_two_pass =
        [&](MSCKFTwoPassFailure failure, std::size_t feature_id,
            const char *stage, int attempted_passes,
            int completed_passes) {
          finalize_ordinary_features();
          PRINT_WARNING(
              YELLOW
              "[MSCKF-ITER]: timestamp=%.17g terminal=1 "
              "requested_passes=2 attempted_passes=%d completed_passes=%d "
              "selected_pass=0 status=rejected stage=%s reason=%s "
              "feature=%zu mean_commits=%zu covariance_commits=%zu "
              "feature_finalizations=%zu\n"
              RESET,
              ordinary_prior.timestamp, attempted_passes, completed_passes,
              stage, msckf_two_pass_failure_name(failure), feature_id,
              ordinary_mean_commit_count, ordinary_covariance_commit_count,
              ordinary_feature_finalization_count);
          ordinary_iteration_terminal_logged = true;
          finish_update(CP2UpdateTerminalStatus::kPreflightRejected,
                        CP2UpdateTerminalSubreason::
                            kBaselinePreflightRejected);
        };

    std::vector<MSCKFUpdatePreviewBlock> pass_one_layout;
    if (!msckf_preview_layout_from_order(Hx_order_big,
                                         pass_one_layout)) {
      reject_two_pass(MSCKFTwoPassFailure::kJacobian, 0,
                      "pass1_layout", 1, 0);
      return;
    }
    const MSCKFPriorSupportFactor prior_factor =
        msckf_factor_prior_support(prior_snapshot.covariance);
    if (!prior_factor.valid) {
      reject_two_pass(MSCKFTwoPassFailure::kPriorSupport, 0,
                      "prior_factor", 1, 0);
      return;
    }
    const MSCKFRectangularMean pass_one_mean =
        msckf_compute_rectangular_mean(
            prior_snapshot, prior_factor, pass_one_layout, Hx_big,
            res_big, _options.sigma_pix);
    if (!pass_one_mean.valid) {
      reject_two_pass(MSCKFTwoPassFailure::kMean, 0, "pass1_mean", 1, 0);
      return;
    }

    const MSCKFTwoPassCost pass_one_cost = msckf_evaluate_true_cost(
        ordinary_state_options, ordinary_prior, pass_one_mean.dx,
        ordinary_feature_batch->CloneActive(), initializer_feat, _options,
        prior_factor);
    if (!pass_one_cost.valid) {
      reject_two_pass(pass_one_cost.failure, pass_one_cost.feature_id,
                      "pass1_cost", 1, 0);
      return;
    }
    const boost::posix_time::ptime pass_one_iteration_end =
        boost::posix_time::microsec_clock::local_time();
    ordinary_completed_passes = 1;
    const double pass_one_processing_ms =
        (pass_one_iteration_end - rT0).total_microseconds() * 1.0e-3;

    MSCKFTwoPassLinearization pass_two;
    MSCKFTwoPassCost pass_two_cost;
    double pass_two_processing_ms =
        std::numeric_limits<double>::quiet_NaN();
    ordinary_attempted_passes = 2;
    try {
      const boost::posix_time::ptime pass_two_iteration_start =
          boost::posix_time::microsec_clock::local_time();
      if (ordinary_two_pass_test_fault ==
          OrdinaryTwoPassTestFault::kPassTwoException) {
        throw std::runtime_error(
            "UpdaterMSCKF injected ordinary Pass-2 exception");
      }
      pass_two = msckf_build_second_pass(
          ordinary_state_options, ordinary_prior, pass_one_mean.dx,
          ordinary_feature_batch->CloneActive(), initializer_feat, _options,
          chi_squared_table, prior_factor);
      const std::size_t injected_feature_id =
          proposal_feature_vec.empty() || !proposal_feature_vec.front()
              ? 0U
              : proposal_feature_vec.front()->featid;
      if (ordinary_two_pass_test_fault ==
              OrdinaryTwoPassTestFault::kPassTwoNonfinite &&
          pass_two.valid && pass_two.H.size() > 0) {
        pass_two.H(0, 0) =
            std::numeric_limits<double>::quiet_NaN();
      }
      if (ordinary_two_pass_test_fault ==
          OrdinaryTwoPassTestFault::kPassTwoGeometryFailure) {
        pass_two = MSCKFTwoPassLinearization();
        pass_two.failure = MSCKFTwoPassFailure::kGeometry;
        pass_two.feature_id = injected_feature_id;
      } else if (ordinary_two_pass_test_fault ==
                 OrdinaryTwoPassTestFault::kPassTwoSchurRankDeficient) {
        pass_two = MSCKFTwoPassLinearization();
        pass_two.failure = MSCKFTwoPassFailure::kSchur;
        pass_two.feature_id = injected_feature_id;
        pass_two.schur_status =
            SchurReductionStatus::kRankDeficient;
        pass_two.schur_stage = SchurReductionStage::kNumericalRank;
        pass_two.singular_values_available = true;
        pass_two.singular_ratio_available = true;
        pass_two.singular_values = Eigen::Vector3d(1.0, 0.5, 0.0);
        pass_two.singular_ratio = 0.0;
      } else if (pass_two.valid &&
                 (!pass_two.H.allFinite() ||
                  !pass_two.residual.allFinite() ||
                  !pass_two.mean.dx.allFinite() ||
                  !std::isfinite(pass_two.mean.nis))) {
        pass_two = MSCKFTwoPassLinearization();
        pass_two.failure = MSCKFTwoPassFailure::kJacobian;
        pass_two.feature_id = injected_feature_id;
      }
      if (pass_two.valid) {
        pass_two_cost = msckf_evaluate_true_cost(
            ordinary_state_options, ordinary_prior, pass_two.mean.dx,
            ordinary_feature_batch->CloneActive(), initializer_feat,
            _options, prior_factor);
      } else {
        pass_two_cost.failure = pass_two.failure;
        pass_two_cost.feature_id = pass_two.feature_id;
      }
      const boost::posix_time::ptime pass_two_iteration_end =
          boost::posix_time::microsec_clock::local_time();
      pass_two_processing_ms =
          (pass_two_iteration_end - pass_two_iteration_start)
              .total_microseconds() *
          1.0e-3;
    } catch (...) {
      pass_two = MSCKFTwoPassLinearization();
      pass_two.failure = MSCKFTwoPassFailure::kException;
      pass_two_cost = MSCKFTwoPassCost();
      pass_two_cost.failure = MSCKFTwoPassFailure::kException;
    }

    int selected_pass = 1;
    const char *selection_reason = "pass2_invalid";
    double pixel_tolerance =
        1.0e-9 * std::max(1.0, std::abs(pass_one_cost.pixel));
    double posterior_tolerance =
        1.0e-9 * std::max(1.0, std::abs(pass_one_cost.posterior));
    if (pass_two.valid && pass_two_cost.valid) {
      const bool pixel_accepted =
          pass_two_cost.pixel <= pass_one_cost.pixel + pixel_tolerance;
      const bool posterior_accepted =
          pass_two_cost.posterior <=
          pass_one_cost.posterior + posterior_tolerance;
      if (pixel_accepted && posterior_accepted) {
        selected_pass = 2;
        selection_reason = "dual_cost_accepted";
      } else if (!pixel_accepted && !posterior_accepted) {
        selection_reason = "both_costs_harmful";
      } else if (!pixel_accepted) {
        selection_reason = "pixel_cost_harmful";
      } else {
        selection_reason = "posterior_cost_harmful";
      }
    } else if (!pass_two.valid) {
      selection_reason = msckf_two_pass_failure_name(pass_two.failure);
    } else {
      selection_reason =
          msckf_two_pass_failure_name(pass_two_cost.failure);
    }
    const int completed_passes =
        pass_two.valid && pass_two_cost.valid ? 2 : 1;
    ordinary_completed_passes = completed_passes;
    ordinary_selected_pass = selected_pass;

    const Eigen::MatrixXd *selected_H = &Hx_big;
    const Eigen::VectorXd *selected_residual = &res_big;
    const std::vector<MSCKFUpdatePreviewBlock> *selected_layout =
        &pass_one_layout;
    const MSCKFRectangularMean *selected_mean = &pass_one_mean;
    const MSCKFTwoPassCost *selected_cost = &pass_one_cost;
    if (selected_pass == 2) {
      selected_H = &pass_two.H;
      selected_residual = &pass_two.residual;
      selected_layout = &pass_two.layout;
      selected_mean = &pass_two.mean;
      selected_cost = &pass_two_cost;
    }

    const MSCKFSelectedCovariance selected_covariance =
        msckf_compute_selected_covariance(
            prior_snapshot, prior_factor, *selected_layout, *selected_H,
            *selected_residual, _options.sigma_pix);
    if (!selected_covariance.valid) {
      PRINT_WARNING(
          YELLOW
          "[MSCKF-ITER]: timestamp=%.17g requested_passes=2 "
          "attempted_passes=2 selected_pass=%d status=rejected "
          "stage=selected_covariance reason=%s symmetry_error=%.17g "
          "symmetry_bound=%.17g lambda_min=%.17g lambda_max=%.17g "
          "psd_bound=%.17g max_diagonal=%.17g "
          "covariance_diagnostics_available=%d "
          "alternative_covariances=0 mean_commits=0 "
          "covariance_commits=0\n"
          RESET,
          ordinary_prior.timestamp, selected_pass,
          msckf_two_pass_failure_name(selected_covariance.failure),
          std::isfinite(selected_covariance.validation.symmetry_error)
              ? selected_covariance.validation.symmetry_error
              : 0.0,
          std::isfinite(selected_covariance.validation.symmetry_bound)
              ? selected_covariance.validation.symmetry_bound
              : 0.0,
          std::isfinite(selected_covariance.validation.minimum_eigenvalue)
              ? selected_covariance.validation.minimum_eigenvalue
              : 0.0,
          std::isfinite(selected_covariance.validation.maximum_eigenvalue)
              ? selected_covariance.validation.maximum_eigenvalue
              : 0.0,
          std::isfinite(selected_covariance.validation.psd_bound)
              ? selected_covariance.validation.psd_bound
              : 0.0,
          std::isfinite(selected_covariance.validation.maximum_diagonal)
              ? selected_covariance.validation.maximum_diagonal
              : 0.0,
          std::isfinite(selected_covariance.validation.symmetry_error) &&
                  std::isfinite(selected_covariance.validation.symmetry_bound) &&
                  std::isfinite(selected_covariance.validation.minimum_eigenvalue) &&
                  std::isfinite(selected_covariance.validation.maximum_eigenvalue) &&
                  std::isfinite(selected_covariance.validation.psd_bound) &&
                  std::isfinite(selected_covariance.validation.maximum_diagonal)
              ? 1
              : 0);
      reject_two_pass(selected_covariance.failure, 0,
                      "selected_covariance", 2, completed_passes);
      return;
    }
    const double selected_mean_error =
        (selected_covariance.dx - selected_mean->dx).norm();
    const double selected_mean_bound =
        1.0e-12 + 1.0e-10 * selected_mean->dx.norm();
    if (!std::isfinite(selected_mean_error) ||
        selected_mean_error > selected_mean_bound) {
      reject_two_pass(MSCKFTwoPassFailure::kMean, 0,
                      "selected_mean_parity", 2, completed_passes);
      return;
    }
    const MSCKFPosteriorValidation &posterior_validation =
        selected_covariance.validation;

    ordinary_feature_batch->AdoptGeometry(selected_cost->geometry);
    ordinary_feature_batch->PrepareFinalization();
    const MSCKFUpdatePriorMatchResult prior_match =
        UpdaterMSCKFPreview::MatchPrior(state, ordinary_prior);
    if (!prior_match.accepted()) {
      PRINT_WARNING(
          YELLOW
          "[MSCKF-PRIOR]: status=%s stage=precommit index=%d "
          "live_writes=0\n"
          RESET,
          msckf_update_prior_match_status_name(prior_match.status),
          (int)prior_match.offending_index);
      feature_vec.clear();
      PRINT_WARNING(
          YELLOW
          "[MSCKF-ITER]: timestamp=%.17g terminal=1 requested_passes=2 "
          "attempted_passes=2 completed_passes=%d selected_pass=0 "
          "status=prior_mismatch reason=snapshot mean_commits=0 "
          "covariance_commits=0 feature_finalizations=0\n"
          RESET,
          ordinary_prior.timestamp, completed_passes);
      ordinary_iteration_terminal_logged = true;
      finish_update(CP2UpdateTerminalStatus::kInternalFailure,
                    CP2UpdateTerminalSubreason::kSnapshotMismatch);
      return;
    }

    std::uint64_t accepted_set_hash = UINT64_C(1469598103934665603);
    for (const auto &feature : proposal_feature_vec) {
      const std::uint64_t id =
          feature ? static_cast<std::uint64_t>(feature->featid) : 0U;
      for (unsigned int byte = 0; byte < 8U; ++byte) {
        accepted_set_hash ^=
            (id >> (8U * byte)) & UINT64_C(0xff);
        accepted_set_hash *= UINT64_C(1099511628211);
      }
    }
    PRINT_DEBUG(
        "[MSCKF-ITER]: timestamp=%.17g requested_passes=2 "
        "attempted_passes=2 completed_passes=%d pass=1 "
        "status=accepted accepted_features=%zu accepted_set_hash=%llu "
        "rows=%d global_proposal_nis=%.17g max_feature_gate_nis=%.17g "
        "threshold_at_max_feature_nis=%.17g Cpix=%.17g Cpost=%.17g "
        "dx_norm=%.17g processing_ms=%.17g "
        "prior_support_error=%.17g prior_rank=%d "
        "prior_zero_tolerance=%.17g prior_lambda_min=%.17g "
        "prior_lambda_max=%.17g\n",
        ordinary_prior.timestamp, completed_passes,
        proposal_feature_vec.size(),
        static_cast<unsigned long long>(accepted_set_hash),
        (int)res_big.rows(), pass_one_mean.nis,
        two_pass_one_max_feature_nis,
        two_pass_one_max_gate_threshold, pass_one_cost.pixel,
        pass_one_cost.posterior, pass_one_mean.dx.norm(),
        pass_one_processing_ms, pass_one_cost.support_error,
        (int)prior_factor.positive_eigenvalues.rows(),
        prior_factor.zero_tolerance, prior_factor.minimum_eigenvalue,
        prior_factor.maximum_eigenvalue);
    if (pass_two.valid && pass_two_cost.valid) {
      PRINT_DEBUG(
          "[MSCKF-ITER]: timestamp=%.17g requested_passes=2 "
          "attempted_passes=2 completed_passes=2 pass=2 "
          "status=accepted accepted_features=%zu "
          "raw_rows=%d rows=%d max_feature_nis=%.17g "
          "threshold_at_max_feature_nis=%.17g "
          "affine_correction_norm=%.17g "
          "Cpix=%.17g Cpost=%.17g dx_norm=%.17g processing_ms=%.17g "
          "repairs=%zu\n",
          ordinary_prior.timestamp, proposal_feature_vec.size(),
          (int)pass_two.raw_rows,
          (int)pass_two.reduced_rows, pass_two.maximum_feature_nis,
          pass_two.maximum_gate_threshold,
          pass_two.affine_correction_norm, pass_two_cost.pixel,
          pass_two_cost.posterior, pass_two.mean.dx.norm(),
          pass_two_processing_ms, pass_two.numerical_repair_count);
    } else {
      const MSCKFTwoPassFailure failure =
          pass_two.valid ? pass_two_cost.failure : pass_two.failure;
      const std::size_t feature_id =
          pass_two.valid ? pass_two_cost.feature_id : pass_two.feature_id;
      if (failure == MSCKFTwoPassFailure::kSchur) {
        PRINT_DEBUG(
            "[MSCKF-ITER]: timestamp=%.17g requested_passes=2 "
            "attempted_passes=2 completed_passes=1 pass=2 "
            "status=invalid reason=%s feature=%zu accepted_features=%zu "
            "schur_status=%s schur_stage=%s singular_values_available=%d "
            "singular_values=[%.17g,%.17g,%.17g] "
            "singular_ratio_available=%d singular_ratio=%.17g "
            "processing_ms=%.17g\n",
            ordinary_prior.timestamp,
            msckf_two_pass_failure_name(failure), feature_id,
            proposal_feature_vec.size(),
            schur_reduction_status_name(pass_two.schur_status),
            schur_reduction_stage_name(pass_two.schur_stage),
            pass_two.singular_values_available ? 1 : 0,
            pass_two.singular_values_available ? pass_two.singular_values(0)
                                               : 0.0,
            pass_two.singular_values_available ? pass_two.singular_values(1)
                                               : 0.0,
            pass_two.singular_values_available ? pass_two.singular_values(2)
                                               : 0.0,
            pass_two.singular_ratio_available ? 1 : 0,
            pass_two.singular_ratio_available ? pass_two.singular_ratio : 0.0,
            pass_two_processing_ms);
      } else {
        PRINT_DEBUG(
            "[MSCKF-ITER]: timestamp=%.17g requested_passes=2 "
            "attempted_passes=2 completed_passes=1 pass=2 "
            "status=invalid reason=%s feature=%zu accepted_features=%zu "
            "gate_diagnostics_available=%d feature_nis=%.17g "
            "threshold_at_feature_nis=%.17g processing_ms=%.17g\n",
            ordinary_prior.timestamp,
            msckf_two_pass_failure_name(failure), feature_id,
            proposal_feature_vec.size(),
            std::isfinite(pass_two.maximum_feature_nis) &&
                    std::isfinite(pass_two.maximum_gate_threshold)
                ? 1
                : 0,
            std::isfinite(pass_two.maximum_feature_nis)
                ? pass_two.maximum_feature_nis
                : 0.0,
            std::isfinite(pass_two.maximum_gate_threshold)
                ? pass_two.maximum_gate_threshold
                : 0.0,
            pass_two_processing_ms);
      }
    }
    PRINT_DEBUG(
        "[MSCKF-ITER]: timestamp=%.17g requested_passes=2 "
        "attempted_passes=2 completed_passes=%d selected_pass=%d "
        "accepted_features=%zu reason=%s "
        "cost_difference_available=%d "
        "pixel_difference=%.17g posterior_difference=%.17g "
        "pixel_tolerance=%.17g posterior_tolerance=%.17g "
        "covariance_symmetry_error=%.17g covariance_symmetry_bound=%.17g "
        "covariance_lambda_min=%.17g covariance_lambda_max=%.17g "
        "covariance_psd_bound=%.17g covariance_max_diagonal=%.17g "
        "planned_mean_commits=1 planned_covariance_commits=1 "
        "mean_commits=%zu covariance_commits=%zu "
        "alternative_covariances=0\n",
        ordinary_prior.timestamp, completed_passes, selected_pass,
        proposal_feature_vec.size(), selection_reason,
        pass_two_cost.valid ? 1 : 0,
        pass_two_cost.valid
            ? pass_one_cost.pixel - pass_two_cost.pixel
            : 0.0,
        pass_two_cost.valid
            ? pass_one_cost.posterior - pass_two_cost.posterior
            : 0.0,
        pixel_tolerance, posterior_tolerance,
        posterior_validation.symmetry_error,
        posterior_validation.symmetry_bound,
        posterior_validation.minimum_eigenvalue,
        posterior_validation.maximum_eigenvalue,
        posterior_validation.psd_bound,
        posterior_validation.maximum_diagonal,
        ordinary_mean_commit_count, ordinary_covariance_commit_count);
    PRINT_ALL(
        "[MSCKF-REDUCTION]: mode=schur accepted_gamma=%.17g "
        "compressed_rows=%d\n",
        retained_gamma, (int)selected_residual->rows());

    update_event.baseline_preflight_accepted = true;
    live_commit_started = true;
#if defined(OV_MSCKF_CP2_TESTING)
    cp2_test_note_stage(CP2UpdaterTestStage::kEKFUpdateEntered);
#endif
    if (!commit_ordinary_update(
            selected_covariance.dx, selected_covariance.posterior,
            StateHelper::PrecomputedCovariancePolicy::NUMERICAL_PSD)) {
      live_commit_started = false;
      PRINT_WARNING(
          YELLOW
          "[MSCKF-COMMIT]: status=invalid_precomputed_update "
          "live_writes=0\n"
          RESET);
      PRINT_WARNING(
          YELLOW
          "[MSCKF-ITER]: timestamp=%.17g requested_passes=2 "
          "attempted_passes=2 completed_passes=%d selected_pass=%d "
          "terminal=1 status=commit_rejected accepted_features=%zu "
          "mean_commits=%zu covariance_commits=%zu "
          "feature_finalizations=%zu\n"
          RESET,
          ordinary_prior.timestamp, completed_passes, selected_pass,
          proposal_feature_vec.size(), ordinary_mean_commit_count,
          ordinary_covariance_commit_count,
          ordinary_feature_finalization_count);
      ordinary_iteration_terminal_logged = true;
      feature_vec.clear();
      finish_update(CP2UpdateTerminalStatus::kInternalFailure,
                    CP2UpdateTerminalSubreason::kTraceInvariantFailure);
      return;
    }
    finalize_ordinary_features();
    if (ordinary_two_pass_test_fault ==
        OrdinaryTwoPassTestFault::kSecondFeatureFinalization) {
      finalize_ordinary_features();
    }
    const CP2SteadyClockEndpoint cp2_update_end = cp2_steady_clock_now();
#if defined(OV_MSCKF_CP2_TESTING)
    cp2_test_note_timing_end();
#endif
    const bool duration_valid = capture_cp2_duration_ns(
        cp2_update_start, cp2_update_end, update_event);
    update_event.terminal_status =
        duration_valid ? CP2UpdateTerminalStatus::kCommittedCounted
                       : CP2UpdateTerminalStatus::kInternalFailure;
    update_event.terminal_subreason =
        duration_valid ? CP2UpdateTerminalSubreason::kNone
                       : CP2UpdateTerminalSubreason::kTraceInvariantFailure;
    update_event.baseline_commit_occurred = true;
    rT5 = boost::posix_time::microsec_clock::local_time();
    notify_observer(update_event, true);
    PRINT_DEBUG(
        "[MSCKF-ITER]: timestamp=%.17g terminal=1 requested_passes=2 "
        "attempted_passes=2 completed_passes=%d selected_pass=%d "
        "status=committed accepted_features=%zu reason=%s "
        "mean_commits=%zu covariance_commits=%zu feature_finalizations=%zu\n",
        ordinary_prior.timestamp, completed_passes, selected_pass,
        proposal_feature_vec.size(), selection_reason,
        ordinary_mean_commit_count, ordinary_covariance_commit_count,
        ordinary_feature_finalization_count);
    ordinary_iteration_terminal_logged = true;

    PRINT_ALL("[MSCKF-UP]: %.4f seconds to clean\n",
              (rT1 - rT0).total_microseconds() * 1e-6);
    PRINT_ALL("[MSCKF-UP]: %.4f seconds to triangulate\n",
              (rT2 - rT1).total_microseconds() * 1e-6);
    PRINT_ALL("[MSCKF-UP]: %.4f seconds create system (%d features)\n",
              (rT3 - rT2).total_microseconds() * 1e-6,
              (int)proposal_feature_vec.size());
    PRINT_ALL("[MSCKF-UP]: %.4f seconds compress system\n",
              (rT4 - rT3).total_microseconds() * 1e-6);
    PRINT_ALL("[MSCKF-UP]: %.4f seconds update state (%d size)\n",
              (rT5 - rT4).total_microseconds() * 1e-6,
              (int)selected_residual->rows());
    PRINT_ALL("[MSCKF-UP]: %.4f seconds total\n",
              (rT5 - rT1).total_microseconds() * 1e-6);
    return;
  }

  // Shared, read-only failure preflight. It is identical in both modes and
  // guarantees invalid global proposals have zero live-state writes.
  update_event.baseline_preflight_attempted = true;
  std::vector<MSCKFUpdatePreviewBlock> baseline_layout;
  try {
    baseline_layout = capture_cp2_preview_layout(Hx_order_big);
  } catch (const std::overflow_error &) {
    if (recorded_mode) {
      finish_recorded_noncommit(
          CP2UpdateTerminalStatus::kInternalFailure,
          CP2UpdateTerminalSubreason::kTraceInvariantFailure);
      return;
    }
    throw;
  }
  const MSCKFUpdatePreviewResult preview =
      UpdaterMSCKFPreview::ComputeFromSnapshot(prior_snapshot, baseline_layout, Hx_big, res_big, R_big);
  if (recorded_mode) {
    const CP2GlobalModeResult &shadow_baseline =
        update_event.shadow.result.nullspace;
    recorded_baseline_provenance_equal =
        recorded_baseline_provenance_equal &&
        cp2_compressed_baseline_equal(shadow_baseline, Hx_big, res_big,
                                      baseline_layout) &&
        shadow_baseline.proposal_available == preview.accepted() &&
        shadow_baseline_proposal_payload_available == preview.accepted() &&
        cp2_preview_diagnostics_equal(shadow_baseline.proposal.diagnostics,
                                      preview.diagnostics);
  }
  if (!preview.accepted()) {
    if (preview.diagnostics.minimum_posterior_diagonal_available) {
      PRINT_WARNING(YELLOW
                    "[MSCKF-PREFLIGHT]: status=%s stage=%s rows=%d min_diagonal=%.17g diagonal_index=%d "
                    "jitter=%zu repair=%zu alternate_solve=%zu clamp=%zu regularization=%zu fallback=%zu\n" RESET,
                    msckf_update_preview_status_name(preview.diagnostics.status),
                    msckf_update_preview_stage_name(preview.diagnostics.stage), (int)res_big.rows(),
                    preview.diagnostics.minimum_posterior_diagonal, (int)preview.diagnostics.offending_diagonal_index,
                    preview.diagnostics.jitter_count, preview.diagnostics.repair_count,
                    preview.diagnostics.alternate_solve_count, preview.diagnostics.clamp_count,
                    preview.diagnostics.regularization_count, preview.diagnostics.fallback_count);
    } else {
      PRINT_WARNING(YELLOW
                    "[MSCKF-PREFLIGHT]: status=%s stage=%s rows=%d min_diagonal=unavailable "
                    "jitter=%zu repair=%zu alternate_solve=%zu clamp=%zu regularization=%zu fallback=%zu\n" RESET,
                    msckf_update_preview_status_name(preview.diagnostics.status),
                    msckf_update_preview_stage_name(preview.diagnostics.stage), (int)res_big.rows(),
                    preview.diagnostics.jitter_count, preview.diagnostics.repair_count,
                    preview.diagnostics.alternate_solve_count, preview.diagnostics.clamp_count,
                    preview.diagnostics.regularization_count, preview.diagnostics.fallback_count);
    }
    if (update_event.shadow_evidence_available) {
      update_event.shadow.baseline_preview_available = true;
      update_event.shadow.live_nullspace_proposal = preview;
    }
    if (recorded_mode) {
      finish_recorded_noncommit(
          CP2UpdateTerminalStatus::kPreflightRejected,
          CP2UpdateTerminalSubreason::kBaselinePreflightRejected);
    } else {
      finalize_ordinary_features();
      finish_update(CP2UpdateTerminalStatus::kPreflightRejected,
                    CP2UpdateTerminalSubreason::kBaselinePreflightRejected);
    }
    return;
  }
  if (!recorded_mode) {
    ordinary_completed_passes = 1;
    ordinary_selected_pass = 1;
  }
  update_event.baseline_preflight_accepted = true;
  if (update_event.shadow_evidence_available) {
    update_event.shadow.baseline_preview_available = true;
    update_event.shadow.live_nullspace_proposal = preview;
    update_event.shadow.baseline_commit_planned = true;
  }

  if (!recorded_mode) {
    // Complete every allocation and pointer-map check before the first live
    // state write. FinalizePrepared is then a no-throw move/swap boundary.
    ordinary_feature_batch->PrepareFinalization();
    const MSCKFUpdatePriorMatchResult prior_match =
        UpdaterMSCKFPreview::MatchPrior(state, ordinary_prior);
    if (!prior_match.accepted()) {
      PRINT_WARNING(YELLOW
                    "[MSCKF-PRIOR]: status=%s stage=precommit index=%d live_writes=0\n" RESET,
                    msckf_update_prior_match_status_name(prior_match.status),
                    (int)prior_match.offending_index);
      feature_vec.clear();
      finish_update(CP2UpdateTerminalStatus::kInternalFailure,
                    CP2UpdateTerminalSubreason::kSnapshotMismatch);
      return;
    }
  }

  PRINT_ALL("[MSCKF-REDUCTION]: mode=%s accepted_gamma=%.17g compressed_rows=%d\n",
            UpdaterOptions::landmark_elimination_as_string(_options.landmark_elimination).c_str(), retained_gamma,
            (int)res_big.rows());

  if (!recorded_mode) {
    // Commit the already validated proposal once. No Kalman solve or candidate
    // injection is repeated at this boundary.
    live_commit_started = true;
#if defined(OV_MSCKF_CP2_TESTING)
    cp2_test_note_stage(CP2UpdaterTestStage::kEKFUpdateEntered);
#endif
    if (!commit_ordinary_update(
            preview.dx, preview.P_plus,
            StateHelper::PrecomputedCovariancePolicy::LEGACY_NONNEGATIVE_DIAGONAL)) {
      live_commit_started = false;
      PRINT_WARNING(YELLOW
                    "[MSCKF-COMMIT]: status=invalid_precomputed_update live_writes=0\n" RESET);
      feature_vec.clear();
      finish_update(CP2UpdateTerminalStatus::kInternalFailure,
                    CP2UpdateTerminalSubreason::kTraceInvariantFailure);
      return;
    }
    finalize_ordinary_features();
    const CP2SteadyClockEndpoint cp2_update_end =
        cp2_steady_clock_now();
#if defined(OV_MSCKF_CP2_TESTING)
    cp2_test_note_timing_end();
#endif
    const bool duration_valid = capture_cp2_duration_ns(
        cp2_update_start, cp2_update_end, update_event);
    update_event.terminal_status =
        duration_valid ? CP2UpdateTerminalStatus::kCommittedCounted
                       : CP2UpdateTerminalStatus::kInternalFailure;
    update_event.terminal_subreason =
        duration_valid ? CP2UpdateTerminalSubreason::kNone
                       : CP2UpdateTerminalSubreason::kTraceInvariantFailure;
    update_event.baseline_commit_occurred = true;
    rT5 = boost::posix_time::microsec_clock::local_time();
    notify_observer(update_event, true);
    PRINT_DEBUG(
        "[MSCKF-ITER]: timestamp=%.17g terminal=1 requested_passes=1 "
        "attempted_passes=1 completed_passes=1 selected_pass=1 "
        "accepted_features=%zu status=committed pass2_attempts=0 "
        "global_proposal_nis=unavailable Cpix=unavailable "
        "Cpost=unavailable dx_norm=%.17g processing_ms=%.17g "
        "mean_commits=%zu covariance_commits=%zu "
        "feature_finalizations=%zu\n",
        ordinary_prior.timestamp, proposal_feature_vec.size(),
        preview.dx.norm(),
        (rT5 - rT0).total_microseconds() * 1.0e-3,
        ordinary_mean_commit_count, ordinary_covariance_commit_count,
        ordinary_feature_finalization_count);
    ordinary_iteration_terminal_logged = true;

    // Debug print timing information
    PRINT_ALL("[MSCKF-UP]: %.4f seconds to clean\n",
              (rT1 - rT0).total_microseconds() * 1e-6);
    PRINT_ALL("[MSCKF-UP]: %.4f seconds to triangulate\n",
              (rT2 - rT1).total_microseconds() * 1e-6);
    PRINT_ALL("[MSCKF-UP]: %.4f seconds create system (%d features)\n",
              (rT3 - rT2).total_microseconds() * 1e-6,
              (int)proposal_feature_vec.size());
    PRINT_ALL("[MSCKF-UP]: %.4f seconds compress system\n",
              (rT4 - rT3).total_microseconds() * 1e-6);
    PRINT_ALL("[MSCKF-UP]: %.4f seconds update state (%d size)\n",
              (rT5 - rT4).total_microseconds() * 1e-6,
              (int)res_big.rows());
    PRINT_ALL("[MSCKF-UP]: %.4f seconds total\n",
              (rT5 - rT1).total_microseconds() * 1e-6);
    return;
  }

  // Bind the accepted live baseline proposal to the independently assembled
  // nullspace proposal before constructing any expected postcommit state.
  std::vector<std::uint8_t> live_baseline_proposal_payload;
  try {
    live_baseline_proposal_payload = CP2TraceCodec::EncodeProposalPayload(
        cp2_proposal_payload(preview));
  } catch (...) {
    latch_cp2_trace_fatal(CP2TraceFatalReason::kRequiredEncoding,
                          "CP2 live baseline proposal encoding failed");
  }
#if defined(OV_MSCKF_CP2_TESTING)
  if (cp2_test_fault ==
          CP2UpdaterTestFault::kBaselineProvenanceMismatch &&
      shadow_baseline_proposal_payload_available &&
      !shadow_baseline_proposal_payload.empty()) {
    shadow_baseline_proposal_payload.back() ^= UINT8_C(1);
  }
#endif
  recorded_baseline_provenance_equal =
      recorded_baseline_provenance_equal &&
      shadow_baseline_proposal_payload_available &&
      shadow_baseline_proposal_payload == live_baseline_proposal_payload;
  recorded_event->baseline_proposal_payload =
      std::move(live_baseline_proposal_payload);
  recorded_event->baseline_proposal_payload_available = true;
  if (!recorded_shadow_trace_valid ||
      !recorded_baseline_provenance_equal) {
    update_event.shadow.baseline_commit_planned = false;
    finish_recorded_noncommit(
        CP2UpdateTerminalStatus::kInternalFailure,
        CP2UpdateTerminalSubreason::kTraceInvariantFailure);
    return;
  }

  // Phase 2 is detached, validated, and encoded while still unpublished.
  CP2CompositeStateSnapshot phase2_snapshot;
  std::uint64_t detached_type_update_calls = 0U;
#if defined(OV_MSCKF_CP2_TESTING)
  cp2_test_note_stage(CP2UpdaterTestStage::kPhase2Build);
#endif
  const CP2CompositeStateStatus phase2_status =
      CP2CompositeStateAdapter::BuildExpected(
          phase0_capture.snapshot, preview, phase2_snapshot,
          detached_type_update_calls);
#if defined(OV_MSCKF_CP2_TESTING)
  if (phase2_status == CP2CompositeStateStatus::kAccepted &&
      cp2_test_fault == CP2UpdaterTestFault::kInvalidPhase2 &&
      phase2_snapshot.covariance.size() > 0) {
    phase2_snapshot.covariance(0, 0) =
        std::numeric_limits<double>::quiet_NaN();
  }
#endif
  if (phase2_status != CP2CompositeStateStatus::kAccepted ||
      CP2CompositeStateAdapter::Validate(phase2_snapshot) !=
          CP2CompositeStateStatus::kAccepted) {
    phase2_snapshot = CP2CompositeStateSnapshot();
    update_event.shadow.baseline_commit_planned = false;
    finish_recorded_noncommit(
        CP2UpdateTerminalStatus::kInternalFailure,
        CP2UpdateTerminalSubreason::kTraceInvariantFailure);
    return;
  }

  std::vector<std::uint8_t> phase2_payload;
  try {
    phase2_payload =
        CP2StateTraceCodec::EncodeSnapshotPayload(phase2_snapshot);
  } catch (...) {
    latch_cp2_trace_fatal(CP2TraceFatalReason::kRequiredEncoding,
                          "CP2 phase 2 payload encoding failed");
  }

  CP2PreparedPostcommitCapture prepared_postcommit;
  if (CP2CompositeStateAdapter::PreparePostcommit(
          phase0_capture, prepared_postcommit) !=
      CP2CompositeStateStatus::kAccepted) {
    phase2_snapshot = CP2CompositeStateSnapshot();
    phase2_payload = std::vector<std::uint8_t>();
    update_event.shadow.baseline_commit_planned = false;
    finish_recorded_noncommit(
        CP2UpdateTerminalStatus::kInternalFailure,
        CP2UpdateTerminalSubreason::kTraceInvariantFailure);
    return;
  }

#if defined(OV_MSCKF_CP2_TESTING)
  if (cp2_test_fault == CP2UpdaterTestFault::kPhase1ValueMismatch) {
    state->_timestamp = std::numeric_limits<double>::quiet_NaN();
  }
#endif

  // Capture and compare phase 1 by exact value bytes before the final pointer
  // proof. A complete difference is representable snapshot-mismatch evidence.
  CP2CompositeStateCapture phase1_capture;
  std::vector<std::uint8_t> phase1_payload;
  bool phase01_canonical_equal = false;
  capture_recorded_phase1(phase1_capture, phase1_payload,
                          phase01_canonical_equal);
  if (!phase01_canonical_equal) {
    const bool pointer_equal = CP2CompositeStateAdapter::PointerGraphMatches(
        state, phase0_capture.pointer_graph);
    phase2_snapshot = CP2CompositeStateSnapshot();
    phase2_payload = std::vector<std::uint8_t>();
    install_recorded_phase01(phase1_capture, phase1_payload, false,
                             pointer_equal);
    update_event.shadow.baseline_commit_planned = false;
    publish_installed_recorded_noncommit(
        CP2UpdateTerminalStatus::kInternalFailure,
        CP2UpdateTerminalSubreason::kSnapshotMismatch);
    return;
  }

  // Allocate and populate every owning precommit record slot before the final
  // pointer proof. Phase 2 stays unpublished until phase 3 is complete.
  install_recorded_phase01(phase1_capture, phase1_payload, true, false);
  try {
    recorded_event->state_phases[2].phase =
        CP2StatePhase::kPhase2ExpectedPostcommit;
    recorded_event->state_phases[2].snapshot.reset(
        new CP2CompositeStateSnapshot(std::move(phase2_snapshot)));
    recorded_event->state_phases[2].payload = std::move(phase2_payload);
    recorded_event->state_phases[3].phase =
        CP2StatePhase::kPhase3LivePostcommit;
  } catch (...) {
    latch_cp2_trace_fatal(CP2TraceFatalReason::kRequiredEncoding,
                          "CP2 could not own the precommit phase buffers");
  }

#if defined(OV_MSCKF_CP2_TESTING)
  if (cp2_test_fault ==
      CP2UpdaterTestFault::kInvalidatePostcommitStorage) {
    CP2CompositeStateAdapter::TestInvalidatePreparedStorage(
        prepared_postcommit);
  } else if (cp2_test_fault ==
             CP2UpdaterTestFault::kInvalidatePostcommitPointerToken) {
    CP2CompositeStateAdapter::TestInvalidatePreparedPointerToken(
        prepared_postcommit);
  }
  if (cp2_test_fault == CP2UpdaterTestFault::kFinalPointerMismatch) {
    if (state->_cam_intrinsics_cameras.empty()) {
      latch_cp2_trace_fatal(CP2TraceFatalReason::kConfigurationInvariant,
                            "CP2 pointer-fault fixture has no camera cache");
    }
    state->_cam_intrinsics_cameras.begin()->second.reset();
  }
#endif

  CP2CompositeStateStatus postcommit_fill_status =
      CP2CompositeStateStatus::kNotPrepared;
  CP2CompositeStateStatus postcommit_handoff_status =
      CP2CompositeStateStatus::kNotPrepared;
  std::unique_ptr<CP2CompositeStateSnapshot> live_phase3_snapshot;
  auto final_pointer_proof = [&state, &phase0_capture]() {
    return CP2CompositeStateAdapter::PointerGraphMatches(
        state, phase0_capture.pointer_graph);
  };
  auto sole_live_commit = [this, &state, &Hx_order_big, &Hx_big, &res_big,
                           &R_big]() {
#if defined(OV_MSCKF_CP2_TESTING)
    cp2_test_note_stage(CP2UpdaterTestStage::kEKFUpdateEntered);
#endif
    StateHelper::EKFUpdate(state, Hx_order_big, Hx_big, res_big, R_big);
  };
  CP2SteadyClock commit_clock;
  auto postcommit_fill_and_handoff =
      [&state, &prepared_postcommit, &live_phase3_snapshot,
       &postcommit_fill_status,
       &postcommit_handoff_status]() noexcept -> CP2PostcommitFillStatus {
    postcommit_fill_status = CP2CompositeStateAdapter::FillPostcommitNoAlloc(
        state, prepared_postcommit);
    if (postcommit_fill_status != CP2CompositeStateStatus::kAccepted) {
      return CP2PostcommitFillStatus::kFailed;
    }
    postcommit_handoff_status =
        CP2CompositeStateAdapter::HandoffPostcommitNoAlloc(
            prepared_postcommit, live_phase3_snapshot);
    return postcommit_handoff_status == CP2CompositeStateStatus::kAccepted
               ? CP2PostcommitFillStatus::kSucceeded
               : CP2PostcommitFillStatus::kFailed;
  };
  CP2CommitBoundaryOutput<CP2SteadyClockEndpoint>
      commit_boundary_output;

  // Set only the exception-classification guard before entering the frozen
  // proof/commit/endpoint/fill boundary. The boundary itself permits no
  // intervening evidence, logging, callback, arithmetic, or state operation.
  live_commit_started = true;
  const CP2CommitBoundaryStatus commit_boundary_status =
      CP2CommitBoundary::run(final_pointer_proof, sole_live_commit,
                             commit_clock, postcommit_fill_and_handoff,
                             commit_boundary_output);
#if defined(OV_MSCKF_CP2_TESTING)
  if (commit_boundary_status != CP2CommitBoundaryStatus::kProofRejected) {
    cp2_test_note_timing_end();
  }
#endif

  if (commit_boundary_status == CP2CommitBoundaryStatus::kProofRejected) {
    live_commit_started = false;
    recorded_event->phase01_pointer_graph_equal = false;
    recorded_event->state_phases[2].snapshot.reset();
    recorded_event->state_phases[2].payload.clear();
    recorded_event->state_phases[3].snapshot.reset();
    recorded_event->state_phases[3].payload.clear();
    recorded_event->state_phase_count = 2U;
    update_event.shadow.baseline_commit_planned = false;
    publish_installed_recorded_noncommit(
        CP2UpdateTerminalStatus::kInternalFailure,
        CP2UpdateTerminalSubreason::kSnapshotMismatch);
    return;
  }
  if (commit_boundary_status !=
          CP2CommitBoundaryStatus::kCommittedFillSucceeded ||
      postcommit_fill_status != CP2CompositeStateStatus::kAccepted ||
      postcommit_handoff_status != CP2CompositeStateStatus::kAccepted ||
      !commit_boundary_output.endpoint_valid ||
      !live_phase3_snapshot) {
    latch_cp2_trace_fatal(CP2TraceFatalReason::kPostcommitCapture,
                          "CP2 postcommit capture or handoff is incomplete");
  }

#if defined(OV_MSCKF_CP2_TESTING)
  if (cp2_test_fault == CP2UpdaterTestFault::kPhase3ValueMismatch &&
      live_phase3_snapshot->covariance.size() > 0) {
    live_phase3_snapshot->covariance(0, 0) = std::nextafter(
        live_phase3_snapshot->covariance(0, 0),
        std::numeric_limits<double>::infinity());
  } else if (cp2_test_fault == CP2UpdaterTestFault::kPhase3Nonfinite &&
             live_phase3_snapshot->covariance.size() > 0) {
    live_phase3_snapshot->covariance(0, 0) =
        std::numeric_limits<double>::quiet_NaN();
  }
#endif

  recorded_event->phase01_pointer_graph_equal = true;
  update_event.terminal_status = CP2UpdateTerminalStatus::kCommittedCounted;
  update_event.terminal_subreason = CP2UpdateTerminalSubreason::kNone;
  update_event.baseline_commit_occurred = true;
  const bool committed_duration_valid =
#if defined(OV_MSCKF_CP2_TESTING)
      cp2_test_fault !=
          CP2UpdaterTestFault::kCommittedDurationArithmeticFailure &&
#endif
      capture_cp2_duration_ns(cp2_update_start,
                              commit_boundary_output.endpoint,
                              update_event);
  if (!committed_duration_valid) {
    latch_cp2_trace_fatal(CP2TraceFatalReason::kArithmeticInvariant,
                          "CP2 committed duration is not representable");
  }

  try {
    recorded_event->state_phases[3].payload =
        CP2StateTraceCodec::EncodeSnapshotPayload(
            *live_phase3_snapshot);
  } catch (...) {
    latch_cp2_trace_fatal(CP2TraceFatalReason::kRequiredEncoding,
                          "CP2 phase 3 payload encoding failed");
  }
  recorded_event->state_phases[3].snapshot =
      std::move(live_phase3_snapshot);
  recorded_event->state_phase_count = 4U;
  recorded_event->baseline_commit_oracle = CP2CommitOracle::Compare(
      *recorded_event->state_phases[0].snapshot,
      *recorded_event->state_phases[2].snapshot,
      *recorded_event->state_phases[3].snapshot,
      detached_type_update_calls);
#if defined(OV_MSCKF_CP2_TESTING)
  if (cp2_test_fault == CP2UpdaterTestFault::kCommitOracleArithmeticOverflow) {
    recorded_event->baseline_commit_oracle = CP2CommitOracleResult{};
    recorded_event->baseline_commit_oracle.status =
        CP2CommitOracleStatus::kArithmeticOverflow;
  } else if (cp2_test_fault ==
             CP2UpdaterTestFault::kCommitOracleInvalidPhase) {
    recorded_event->baseline_commit_oracle.status =
        CP2CommitOracleStatus::kInvalidPhase;
  }
#endif
  if (recorded_event->baseline_commit_oracle.status ==
      CP2CommitOracleStatus::kArithmeticOverflow) {
    latch_cp2_trace_fatal(CP2TraceFatalReason::kArithmeticInvariant,
                          "CP2 commit-oracle arithmetic overflowed");
  }
  if (recorded_event->baseline_commit_oracle.status ==
      CP2CommitOracleStatus::kInvalidPhase) {
    latch_cp2_trace_fatal(CP2TraceFatalReason::kPostcommitException,
                          "CP2 commit-oracle phase input is invalid");
  }
  recorded_event->baseline_commit_oracle_available = true;
  recorded_event->baseline_commit_mismatch =
      !recorded_event->baseline_commit_oracle.passed;
  recorded_event->baseline_commit_count = 1U;
  recorded_event->baseline_mean_commit_count = 1U;
  recorded_event->baseline_covariance_commit_count = 1U;
  recorded_event->online_math_evidence_passed =
      recorded_shadow_trace_valid && recorded_shadow_math_valid &&
      recorded_baseline_provenance_equal &&
      recorded_event->candidate_proposal_payload_available &&
      update_event.baseline_gamma_status != CP2GammaStatus::kNonfinite &&
      update_event.shadow.result.schur.gamma_status !=
          CP2GammaStatus::kNonfinite &&
      recorded_event->baseline_commit_oracle.passed;
  recorded_event->update = std::move(update_event);
  publish_record();
  return;
  } catch (const CP2TraceFatalError &) {
    throw;
  } catch (const std::overflow_error &exception) {
    if (recorded_mode) {
      latch_cp2_trace_fatal(
          CP2TraceFatalReason::kArithmeticInvariant,
          live_commit_started
              ? "CP2 checked arithmetic failed after live commit entry"
              : "CP2 checked arithmetic made the authoritative trace incomplete");
    }
    if (live_commit_started) {
      PRINT_ERROR(RED "[MSCKF-CP2]: checked arithmetic failed after live commit entry: %s\n" RESET,
                  exception.what());
      throw;
    }
    PRINT_WARNING(YELLOW "[MSCKF-CP2]: status=internal_failure subreason=trace_invariant_failure detail=%s\n" RESET,
                  exception.what());
    if (!recorded_mode) {
      feature_vec.clear();
    }
    finish_update(CP2UpdateTerminalStatus::kInternalFailure,
                  CP2UpdateTerminalSubreason::kTraceInvariantFailure);
  } catch (const std::exception &exception) {
    if (recorded_mode) {
      latch_cp2_trace_fatal(
          live_commit_started ? CP2TraceFatalReason::kPostcommitException
                              : CP2TraceFatalReason::kRequiredEncoding,
          live_commit_started
              ? "CP2 exception after live commit entry"
              : "CP2 authoritative update trace is incomplete");
    }
    if (live_commit_started) {
      PRINT_ERROR(RED "[MSCKF-CP2]: exception after live commit entry; refusing to classify a potentially partial commit: %s\n" RESET,
                  exception.what());
      throw;
    }
    PRINT_WARNING(YELLOW "[MSCKF-CP2]: status=internal_failure subreason=trace_invariant_failure detail=%s\n" RESET,
                  exception.what());
    if (!recorded_mode) {
      feature_vec.clear();
    }
    finish_update(CP2UpdateTerminalStatus::kInternalFailure,
                  CP2UpdateTerminalSubreason::kTraceInvariantFailure);
  } catch (...) {
    if (recorded_mode) {
      latch_cp2_trace_fatal(
          live_commit_started ? CP2TraceFatalReason::kPostcommitException
                              : CP2TraceFatalReason::kRequiredEncoding,
          live_commit_started
              ? "CP2 unknown exception after live commit entry"
              : "CP2 authoritative update trace failed with an unknown exception");
    }
    if (live_commit_started) {
      PRINT_ERROR(RED
                  "[MSCKF-CP2]: unknown exception after live commit entry; refusing to classify a potentially partial commit\n" RESET);
      throw;
    }
    PRINT_WARNING(YELLOW
                  "[MSCKF-CP2]: status=internal_failure subreason=trace_invariant_failure detail=unknown\n" RESET);
    if (!recorded_mode) {
      feature_vec.clear();
    }
    finish_update(CP2UpdateTerminalStatus::kInternalFailure,
                  CP2UpdateTerminalSubreason::kTraceInvariantFailure);
  }
}
