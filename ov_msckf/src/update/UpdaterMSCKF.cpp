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

#include "CP2FeatureGate.h"
#include "SchurUpdate.h"
#include "UpdaterHelper.h"
#include "UpdaterMSCKFPreview.h"

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

#include <chrono>
#include <cmath>
#include <cstdlib>
#include <exception>
#include <limits>
#include <ratio>
#include <stdexcept>
#include <utility>

using namespace ov_core;
using namespace ov_type;
using namespace ov_msckf;

namespace {

static_assert(std::ratio_equal<std::chrono::steady_clock::period, std::nano>::value,
              "CP2 requires a nanosecond steady clock");
static_assert(std::numeric_limits<std::chrono::steady_clock::duration::rep>::is_signed,
              "CP2 steady-clock tick count must be signed");
static_assert(std::numeric_limits<std::chrono::steady_clock::duration::rep>::digits <=
                  std::numeric_limits<std::uint64_t>::digits,
              "CP2 steady-clock tick count must fit u64");

bool capture_cp2_duration_ns(const std::chrono::steady_clock::time_point &start,
                             const std::chrono::steady_clock::time_point &end,
                             std::uint64_t &duration_ns) noexcept {
  const std::chrono::steady_clock::duration elapsed = end - start;
  if (elapsed < std::chrono::steady_clock::duration::zero()) {
    duration_ns = 0;
    return false;
  }
  duration_ns = static_cast<std::uint64_t>(elapsed.count());
  return true;
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
    layout.push_back({variable->id(), variable->size(), H_offset});
    H_offset += variable->size();
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
    layout.push_back({variable->id(), variable->size(), H_offset});
    H_offset += variable->size();
  }
  return layout;
}

} // namespace

UpdaterMSCKF::UpdaterMSCKF(UpdaterOptions &options, ov_core::FeatureInitializerOptions &feat_init_options) : _options(options) {

  // Preserve the startup contract for direct/API construction as well: the
  // exact variance used by every gate and update must remain finite and >0.
  const double sigma_pix_sq = _options.sigma_pix * _options.sigma_pix;
  if (!UpdaterOptions::landmark_elimination_is_supported(_options.landmark_elimination) ||
      !std::isfinite(_options.sigma_pix) || !(_options.sigma_pix > 0.0) || !std::isfinite(sigma_pix_sq) ||
      !(sigma_pix_sq > 0.0) || !std::isfinite(_options.chi2_multipler)) {
    PRINT_ERROR(RED
                "invalid MSCKF updater configuration: mode=%s sigma_px=%.17g sigma_px_sq=%.17g "
                "chi2_multiplier=%.17g\n" RESET,
                UpdaterOptions::landmark_elimination_as_string(_options.landmark_elimination).c_str(), _options.sigma_pix,
                sigma_pix_sq, _options.chi2_multipler);
    std::exit(EXIT_FAILURE);
  }
  _options.sigma_pix_sq = sigma_pix_sq;

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
  if (cp2_callback_configuration_frozen) {
    return false;
  }
  cp2_update_callback = std::move(installed_callback);
  cp2_shadow_enabled = enable_shadow;
  return true;
}

void UpdaterMSCKF::update(std::shared_ptr<State> state, std::vector<std::shared_ptr<Feature>> &feature_vec) {

  const std::chrono::steady_clock::time_point cp2_update_start = std::chrono::steady_clock::now();
  bool expected_inactive = false;
  if (!cp2_update_active.compare_exchange_strong(expected_inactive, true, std::memory_order_acquire,
                                                  std::memory_order_relaxed)) {
    throw std::logic_error("UpdaterMSCKF::update requires serialized, nonreentrant use");
  }
  const CP2UpdateActivityGuard activity_guard(cp2_update_active);
  std::shared_ptr<const CP2UpdateCallback> update_callback;
  bool shadow_requested = false;
  {
    const std::lock_guard<std::mutex> lock(cp2_callback_mutex);
    cp2_callback_configuration_frozen = true;
    update_callback = cp2_update_callback;
    shadow_requested = cp2_shadow_enabled;
  }
  CP2LiveUpdateEvent update_event;
  update_event.input_feature_count = static_cast<std::uint64_t>(feature_vec.size());
  const auto finish_update = [&update_callback, &update_event, &cp2_update_start](
                                 CP2UpdateTerminalStatus status,
                                 CP2UpdateTerminalSubreason terminal_subreason) {
    const std::chrono::steady_clock::time_point cp2_update_end = std::chrono::steady_clock::now();
    const bool duration_valid =
        capture_cp2_duration_ns(cp2_update_start, cp2_update_end, update_event.duration_ns);
    update_event.terminal_status = duration_valid ? status : CP2UpdateTerminalStatus::kInternalFailure;
    update_event.terminal_subreason =
        duration_valid ? terminal_subreason : CP2UpdateTerminalSubreason::kTraceInvariantFailure;
    if (!update_callback || !*update_callback) {
      return;
    }
    try {
      (*update_callback)(std::move(update_event));
    } catch (const std::exception &exception) {
      PRINT_WARNING(YELLOW "[MSCKF-CP2-OBSERVER]: status=callback_exception detail=%s baseline_unchanged=1\n" RESET,
                    exception.what());
    } catch (...) {
      PRINT_WARNING(YELLOW
                    "[MSCKF-CP2-OBSERVER]: status=callback_exception detail=unknown baseline_unchanged=1\n" RESET);
    }
  };
  bool live_commit_started = false;

  try {

  // Return if no features
  if (feature_vec.empty()) {
    finish_update(CP2UpdateTerminalStatus::kEmptyInput,
                  CP2UpdateTerminalSubreason::kInputEmpty);
    return;
  }

  // Start timing
  boost::posix_time::ptime rT0, rT1, rT2, rT3, rT4, rT5;
  rT0 = boost::posix_time::microsec_clock::local_time();

  // 0. Get all timestamps our clones are at (and thus valid measurement times)
  std::vector<double> clonetimes;
  for (const auto &clone_imu : state->_clones_IMU) {
    clonetimes.emplace_back(clone_imu.first);
  }

  // 1. Clean all feature measurements and make sure they all have valid clone times
  auto it0 = feature_vec.begin();
  while (it0 != feature_vec.end()) {

    // Clean the feature
    (*it0)->clean_old_measurements(clonetimes);

    // Count how many measurements
    int ct_meas = 0;
    for (const auto &pair : (*it0)->timestamps) {
      ct_meas += (*it0)->timestamps[pair.first].size();
    }

    // Remove if we don't have enough
    if (ct_meas < 2) {
      (*it0)->to_delete = true;
      it0 = feature_vec.erase(it0);
    } else {
      it0++;
    }
  }
  rT1 = boost::posix_time::microsec_clock::local_time();
  const bool features_after_cleaning = !feature_vec.empty();

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
  auto it1 = feature_vec.begin();
  while (it1 != feature_vec.end()) {

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
      it1 = feature_vec.erase(it1);
      continue;
    }
    it1++;
  }
  rT2 = boost::posix_time::microsec_clock::local_time();
  const bool features_after_triangulation = !feature_vec.empty();

  // Calculate the max possible measurement size
  size_t max_meas_size = 0;
  for (size_t i = 0; i < feature_vec.size(); i++) {
    for (const auto &pair : feature_vec.at(i)->timestamps) {
      max_meas_size += 2 * feature_vec.at(i)->timestamps[pair.first].size();
    }
  }

  // Calculate max possible state size (i.e. the size of our covariance)
  // NOTE: that when we have the single inverse depth representations, those are only 1dof in size
  size_t max_hx_size = state->max_covariance_size();
  for (auto &landmark : state->_features_SLAM) {
    max_hx_size -= landmark.second->size();
  }

  // Large Jacobian and residual of *all* features for this update
  Eigen::VectorXd res_big = Eigen::VectorXd::Zero(max_meas_size);
  Eigen::MatrixXd Hx_big = Eigen::MatrixXd::Zero(max_meas_size, max_hx_size);
  std::unordered_map<std::shared_ptr<Type>, size_t> Hx_mapping;
  std::vector<std::shared_ptr<Type>> Hx_order_big;
  size_t ct_jacob = 0;
  size_t ct_meas = 0;
  double retained_gamma = 0.0;
  bool retained_gamma_available = true;

  // The full prior is immutable throughout feature reduction and gating. It
  // is captured once so the live shared gate and both opt-in shadow proposals
  // consume exactly the same covariance bytes.
  const MSCKFUpdatePreviewSnapshot prior_snapshot = UpdaterMSCKFPreview::CaptureSnapshot(state);
  const bool observer_enabled = static_cast<bool>(update_callback);
  const bool shadow_enabled =
      observer_enabled && shadow_requested &&
      _options.landmark_elimination == UpdaterOptions::LandmarkElimination::NULLSPACE;
  CP2ShadowMathInput shadow_input;
  if (shadow_enabled) {
    shadow_input.prior = prior_snapshot;
    shadow_input.sigma_px = _options.sigma_pix;
    shadow_input.sigma_px_sq = _options.sigma_pix_sq;
    shadow_input.chi2_multiplier = _options.chi2_multipler;
    shadow_input.chi_squared_table = chi_squared_table;
    shadow_input.raw_systems.reserve(feature_vec.size());
  }

  // 4. Compute linear system for each feature, nullspace project, and reject
  auto it2 = feature_vec.begin();
  while (it2 != feature_vec.end()) {

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

    // Get the Jacobian for this feature
    UpdaterHelper::get_feature_jacobian_full(state, feat, H_f, H_x, res, Hx_order);
    ++update_event.raw_system_count;

    // This is the sole shared raw seam: owning copies and value-only layout
    // are captured immediately after production assembly and before either
    // reducer can mutate H_f, H_x, or res.
    const std::vector<CP2FeatureGateLayoutBlock> feature_layout = capture_cp2_feature_layout(Hx_order);
    if (shadow_enabled) {
      CP2RawFeatureSystem raw;
      raw.feature_id = static_cast<std::uint64_t>(feat.featid);
      raw.H_x = H_x;
      raw.H_f = H_f;
      raw.residual = res;
      raw.jacobian_layout = feature_layout;
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
        it2 = feature_vec.erase(it2);
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
      finish_update(CP2UpdateTerminalStatus::kInternalFailure,
                    CP2UpdateTerminalSubreason::kInvalidLiveMode);
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
      it2 = feature_vec.erase(it2);
      continue;
    }
    update_event.baseline_accepted_ids.push_back(static_cast<std::uint64_t>(feat.featid));
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

    // We are good!!! Append to our large H vector
    size_t ct_hx = 0;
    for (const auto &var : Hx_order) {

      // Ensure that this variable is in our Jacobian
      if (Hx_mapping.find(var) == Hx_mapping.end()) {
        Hx_mapping.insert({var, ct_jacob});
        Hx_order_big.push_back(var);
        ct_jacob += var->size();
      }

      // Append to our large Jacobian
      Hx_big.block(ct_meas, Hx_mapping[var], H_x.rows(), var->size()) = H_x.block(0, ct_hx, H_x.rows(), var->size());
      ct_hx += var->size();
    }

    // Append our residual and move forward
    res_big.block(ct_meas, 0, res.rows(), 1) = res;
    ct_meas += res.rows();
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
      update_event.shadow.shadow_math_completed = true;
    } catch (const std::exception &exception) {
      PRINT_WARNING(YELLOW "[MSCKF-CP2-SHADOW]: status=exception detail=%s baseline_unchanged=1\n" RESET,
                    exception.what());
    } catch (...) {
      PRINT_WARNING(YELLOW "[MSCKF-CP2-SHADOW]: status=exception detail=unknown baseline_unchanged=1\n" RESET);
    }
  }

  // We have appended all features to our Hx_big, res_big
  // Delete it so we do not reuse information
  for (size_t f = 0; f < feature_vec.size(); f++) {
    feature_vec[f]->to_delete = true;
  }

  if (update_event.raw_system_count > 0) {
    update_event.baseline_precompression_rows_available = true;
    update_event.baseline_precompression_rows = static_cast<std::uint64_t>(ct_meas);
    update_event.baseline_precompression_system_nonempty = ct_meas > 0;
  }

  // Return if we don't have anything and resize our matrices
  if (ct_meas < 1) {
    CP2UpdateTerminalSubreason terminal_subreason =
        CP2UpdateTerminalSubreason::kAllBaselineFeaturesRejected;
    if (!features_after_cleaning) {
      terminal_subreason = CP2UpdateTerminalSubreason::kNoFeaturesAfterCleaning;
    } else if (!features_after_triangulation) {
      terminal_subreason = CP2UpdateTerminalSubreason::kNoFeaturesAfterTriangulation;
    } else if (update_event.raw_system_count == 0) {
      terminal_subreason = CP2UpdateTerminalSubreason::kNoRawSystems;
    }
    finish_update(CP2UpdateTerminalStatus::kAllRejected, terminal_subreason);
    return;
  }
  assert(ct_meas <= max_meas_size);
  assert(ct_jacob <= max_hx_size);
  res_big.conservativeResize(ct_meas, 1);
  Hx_big.conservativeResize(ct_meas, ct_jacob);

  // 5. Perform measurement compression
  UpdaterHelper::measurement_compress_inplace(Hx_big, res_big);
  update_event.baseline_compressed_rows_available = true;
  update_event.baseline_compressed_rows = static_cast<std::uint64_t>(Hx_big.rows());
  if (Hx_big.rows() < 1) {
    finish_update(CP2UpdateTerminalStatus::kEmptyAfterCompression,
                  CP2UpdateTerminalSubreason::kMeasurementCompressionEmpty);
    return;
  }
  update_event.baseline_compressed_system_nonempty = true;
  rT4 = boost::posix_time::microsec_clock::local_time();

  // Our noise is isotropic, so make it here after our compression
  Eigen::MatrixXd R_big = _options.sigma_pix_sq * Eigen::MatrixXd::Identity(res_big.rows(), res_big.rows());

  // Shared, read-only failure preflight. It is identical in both modes and
  // guarantees invalid global proposals have zero live-state writes.
  update_event.baseline_preflight_attempted = true;
  const std::vector<MSCKFUpdatePreviewBlock> baseline_layout = capture_cp2_preview_layout(Hx_order_big);
  const MSCKFUpdatePreviewResult preview =
      UpdaterMSCKFPreview::ComputeFromSnapshot(prior_snapshot, baseline_layout, Hx_big, res_big, R_big);
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
    finish_update(CP2UpdateTerminalStatus::kPreflightRejected,
                  CP2UpdateTerminalSubreason::kBaselinePreflightRejected);
    return;
  }
  update_event.baseline_preflight_accepted = true;
  if (update_event.shadow_evidence_available) {
    update_event.shadow.baseline_preview_available = true;
    update_event.shadow.live_nullspace_proposal = preview;
    update_event.shadow.baseline_commit_planned = true;
  }

  PRINT_ALL("[MSCKF-REDUCTION]: mode=%s accepted_gamma=%.17g compressed_rows=%d\n",
            UpdaterOptions::landmark_elimination_as_string(_options.landmark_elimination).c_str(), retained_gamma,
            (int)res_big.rows());

  // 6. With all good features update the state
  live_commit_started = true;
  StateHelper::EKFUpdate(state, Hx_order_big, Hx_big, res_big, R_big);
  const std::chrono::steady_clock::time_point cp2_update_end = std::chrono::steady_clock::now();
  const bool duration_valid =
      capture_cp2_duration_ns(cp2_update_start, cp2_update_end, update_event.duration_ns);
  update_event.terminal_status = duration_valid ? CP2UpdateTerminalStatus::kCommittedCounted
                                                : CP2UpdateTerminalStatus::kInternalFailure;
  update_event.terminal_subreason = duration_valid ? CP2UpdateTerminalSubreason::kNone
                                                    : CP2UpdateTerminalSubreason::kTraceInvariantFailure;
  update_event.baseline_commit_occurred = true;
  rT5 = boost::posix_time::microsec_clock::local_time();

  if (update_callback && *update_callback) {
    try {
      (*update_callback)(std::move(update_event));
    } catch (const std::exception &exception) {
      PRINT_WARNING(YELLOW "[MSCKF-CP2-OBSERVER]: status=callback_exception detail=%s baseline_committed=1\n" RESET,
                    exception.what());
    } catch (...) {
      PRINT_WARNING(YELLOW
                    "[MSCKF-CP2-OBSERVER]: status=callback_exception detail=unknown baseline_committed=1\n" RESET);
    }
  }

  // Debug print timing information
  PRINT_ALL("[MSCKF-UP]: %.4f seconds to clean\n", (rT1 - rT0).total_microseconds() * 1e-6);
  PRINT_ALL("[MSCKF-UP]: %.4f seconds to triangulate\n", (rT2 - rT1).total_microseconds() * 1e-6);
  PRINT_ALL("[MSCKF-UP]: %.4f seconds create system (%d features)\n", (rT3 - rT2).total_microseconds() * 1e-6, (int)feature_vec.size());
  PRINT_ALL("[MSCKF-UP]: %.4f seconds compress system\n", (rT4 - rT3).total_microseconds() * 1e-6);
  PRINT_ALL("[MSCKF-UP]: %.4f seconds update state (%d size)\n", (rT5 - rT4).total_microseconds() * 1e-6, (int)res_big.rows());
  PRINT_ALL("[MSCKF-UP]: %.4f seconds total\n", (rT5 - rT1).total_microseconds() * 1e-6);
  } catch (const std::exception &exception) {
    if (live_commit_started) {
      PRINT_ERROR(RED "[MSCKF-CP2]: exception after live commit entry; refusing to classify a potentially partial commit: %s\n" RESET,
                  exception.what());
      throw;
    }
    PRINT_WARNING(YELLOW "[MSCKF-CP2]: status=internal_failure subreason=trace_invariant_failure detail=%s\n" RESET,
                  exception.what());
    finish_update(CP2UpdateTerminalStatus::kInternalFailure,
                  CP2UpdateTerminalSubreason::kTraceInvariantFailure);
  } catch (...) {
    if (live_commit_started) {
      PRINT_ERROR(RED
                  "[MSCKF-CP2]: unknown exception after live commit entry; refusing to classify a potentially partial commit\n" RESET);
      throw;
    }
    PRINT_WARNING(YELLOW
                  "[MSCKF-CP2]: status=internal_failure subreason=trace_invariant_failure detail=unknown\n" RESET);
    finish_update(CP2UpdateTerminalStatus::kInternalFailure,
                  CP2UpdateTerminalSubreason::kTraceInvariantFailure);
  }
}
