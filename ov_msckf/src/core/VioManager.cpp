/*
 * OpenVINS: An Open Platform for Visual-Inertial Research
 * Copyright (C) 2018-2023 Patrick Geneva
 * Copyright (C) 2018-2023 Guoquan Huang
 * Copyright (C) 2018-2023 OpenVINS Contributors
 * Copyright (C) 2018-2019 Kevin Eckenhoff
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

#include "VioManager.h"
#include "LongGapRelocalizer.h"
#include "TurnSafeCallbackInstaller.h"

#include "feat/Feature.h"
#include "feat/FeatureDatabase.h"
#include "feat/FeatureInitializer.h"
#include "track/TrackAruco.h"
#include "track/TrackDescriptor.h"
#include "track/TrackKLT.h"
#include "track/TrackSIM.h"
#include "types/Landmark.h"
#include "types/LandmarkRepresentation.h"
#include "utils/opencv_lambda_body.h"
#include "utils/print.h"
#include "utils/sensor_data.h"

#include "init/InertialInitializer.h"

#include "state/Propagator.h"
#include "state/State.h"
#include "state/StateHelper.h"
#include "update/UpdaterMSCKF.h"
#include "update/CP2OutputCapability.h"
#include "update/TurnSafeDiagnostics.h"
#include "update/UpdaterSLAM.h"
#include "update/UpdaterZeroVelocity.h"

#include <cmath>
#include <exception>
#include <limits>
#include <new>
#include <stdexcept>
#include <utility>

using namespace ov_core;
using namespace ov_type;
using namespace ov_msckf;

namespace {

class TurnSafeCallbackEnvelope {
public:
  TurnSafeCallbackEnvelope(
      const std::shared_ptr<TurnSafeDiagnostics> &diagnostics,
      std::shared_ptr<const TrackKLT> tracker,
      std::shared_ptr<const UpdaterMSCKF> updater,
      double timestamp, const std::vector<int> &camera_frame_ids,
      const std::shared_ptr<State> *state_owner, Propagator *propagator,
      const bool *estimator_initialized,
      const double *last_update_timestamp) noexcept
      : diagnostics_(diagnostics), tracker_(std::move(tracker)),
        updater_(std::move(updater)), state_owner_(state_owner),
        estimator_initialized_(estimator_initialized),
        last_update_timestamp_(last_update_timestamp) {
    PollComponentFailures();
    if (diagnostics_ && diagnostics_->active()) {
      diagnostics_->BeginCallback(
          timestamp, camera_frame_ids,
          estimator_initialized_ != nullptr && *estimator_initialized_,
          OutputReady(), CurrentState(), propagator);
    }
  }

  ~TurnSafeCallbackEnvelope() noexcept {
    PollComponentFailures();
    if (diagnostics_) {
      diagnostics_->EndCallback(
          estimator_initialized_ != nullptr && *estimator_initialized_,
          OutputReady(), CurrentState(), std::uncaught_exception());
    }
  }

private:
  const State *CurrentState() const noexcept {
    return state_owner_ == nullptr ? nullptr : state_owner_->get();
  }

  bool OutputReady() const noexcept {
    return estimator_initialized_ != nullptr && *estimator_initialized_ &&
           last_update_timestamp_ != nullptr && *last_update_timestamp_ != -1.0;
  }

  void PollComponentFailures() noexcept {
    if (!diagnostics_ || !diagnostics_->active()) return;
    if (tracker_ && tracker_->turnsafe_diagnostic_failure_reason() !=
                        TrackKLTDiagnosticFailureReason::kNone) {
      diagnostics_->ReportComponentFailure(
          TurnSafeCaptureDisableReason::kFrontendCaptureFailure);
      return;
    }
    if (updater_ && updater_->turnsafe_t0_failure_reason() !=
                        TurnSafeUpdaterDiagnosticFailureReason::kNone) {
      diagnostics_->ReportComponentFailure(
          TurnSafeCaptureDisableReason::kUpdaterCaptureFailure);
    }
  }

  std::shared_ptr<TurnSafeDiagnostics> diagnostics_;
  // Recovery replaces the frontend and state inside the callback.  Retain
  // the frontend/updater whose diagnostic hooks served this callback, while
  // resolving the state slot again at callback end so metadata describes the
  // committed state rather than a destroyed predecessor.
  std::shared_ptr<const TrackKLT> tracker_;
  std::shared_ptr<const UpdaterMSCKF> updater_;
  const std::shared_ptr<State> *state_owner_ = nullptr;
  const bool *estimator_initialized_ = nullptr;
  const double *last_update_timestamp_ = nullptr;
};

} // namespace

TurnSafeCallbackInstallationResult ov_msckf::install_turnsafe_t0_callbacks(
    const std::shared_ptr<TurnSafeDiagnostics> &diagnostics,
    TrackKLT *tracker, UpdaterMSCKF *updater) noexcept {
  TurnSafeCallbackInstallationResult result;
  if (!diagnostics || !diagnostics->active()) return result;

  const std::weak_ptr<TurnSafeDiagnostics> weak_sink = diagnostics;
  bool tracker_installed = tracker == nullptr;
  bool updater_installed = false;
  result.failure_reason =
      TurnSafeCaptureDisableReason::kFrontendCaptureFailure;
  try {
    // std::function construction itself may allocate before a by-value setter
    // can begin executing, so construction and installation share one guard.
    if (tracker) {
      inject_turnsafe_diagnostic_fault_for_test(
          TurnSafeDiagnosticFaultStage::kTrackerCallbackInstallation);
      TrackKLT::DiagnosticsCallback tracker_callback(
          [weak_sink](TrackKLTFrameDiagnostics record) {
            const auto sink = weak_sink.lock();
            if (sink) sink->RecordFrontend(std::move(record));
          });
      tracker_installed = tracker->set_turnsafe_diagnostics_callback(
          std::move(tracker_callback));
    }
    if (tracker_installed) {
      result.failure_reason =
          TurnSafeCaptureDisableReason::kUpdaterCaptureFailure;
      if (updater) {
        inject_turnsafe_diagnostic_fault_for_test(
            TurnSafeDiagnosticFaultStage::kUpdaterCallbackInstallation);
        UpdaterMSCKF::TurnSafeT0Callback updater_callback(
            [weak_sink](TurnSafeUpdateRecord record) {
              const auto sink = weak_sink.lock();
              if (sink) sink->RecordUpdate(std::move(record));
            });
        updater_installed = updater->set_turnsafe_t0_callback(
            std::move(updater_callback));
      }
    }
  } catch (const std::bad_alloc &) {
  } catch (const std::exception &) {
  } catch (...) {
  }

  if (tracker_installed && updater_installed) {
    result.success = true;
    result.tracker_callback_retained = tracker != nullptr;
    result.updater_callback_retained = true;
    result.failure_reason = TurnSafeCaptureDisableReason::kNone;
    return result;
  }

  // Remove whichever half installed successfully. The empty callbacks do not
  // allocate; each setter contains its own lock/assignment failure boundary.
  if (tracker && tracker_installed) {
    result.tracker_rollback_succeeded =
        tracker->set_turnsafe_diagnostics_callback({});
    result.tracker_callback_retained =
        !result.tracker_rollback_succeeded;
  }
  if (updater && updater_installed) {
    result.updater_rollback_succeeded =
        updater->set_turnsafe_t0_callback({});
    result.updater_callback_retained =
        !result.updater_rollback_succeeded;
  }
  diagnostics->ReportComponentFailure(result.failure_reason);
  return result;
}

bool ov_msckf::reinstall_turnsafe_t0_frontend_callback(
    const std::shared_ptr<TurnSafeDiagnostics> &diagnostics,
    TrackKLT *tracker) noexcept {
  if (!diagnostics || !diagnostics->active()) return true;
  bool installed = false;
  try {
    if (tracker != nullptr) {
      const std::weak_ptr<TurnSafeDiagnostics> weak_sink = diagnostics;
      inject_turnsafe_diagnostic_fault_for_test(
          TurnSafeDiagnosticFaultStage::kTrackerCallbackInstallation);
      TrackKLT::DiagnosticsCallback tracker_callback(
          [weak_sink](TrackKLTFrameDiagnostics record) {
            const auto sink = weak_sink.lock();
            if (sink) sink->RecordFrontend(std::move(record));
          });
      installed = tracker->set_turnsafe_diagnostics_callback(
          std::move(tracker_callback));
    }
  } catch (const std::bad_alloc &) {
  } catch (const std::exception &) {
  } catch (...) {
  }
  if (!installed) {
    diagnostics->ReportComponentFailure(
        TurnSafeCaptureDisableReason::kFrontendCaptureFailure);
  }
  return installed;
}

VioManager::VioManager(VioManagerOptions &params_) : thread_init_running(false), thread_init_success(false) {

  // Nice startup message
  PRINT_DEBUG("=======================================\n");
  PRINT_DEBUG("OPENVINS ON-MANIFOLD EKF IS STARTING\n");
  PRINT_DEBUG("=======================================\n");

  // Nice debug
  this->params = params_;
  params.print_and_load_estimator();
  params.print_and_load_noise();
  params.print_and_load_state();
  params.print_and_load_trackers();

  // The recovery path is deliberately narrower than ordinary OpenVINS.  It
  // is a frozen KAIST robustness experiment, default-off, and must not turn
  // into a configurable gate search or silently run under a different camera
  // model/frontend contract.
  if (params.long_gap_recovery_enabled) {
    const bool frozen_recovery_contract =
        params.use_klt && params.use_stereo && !params.downsample_cameras &&
        !params.use_aruco && params.state_options.num_cameras == 2 &&
        !params.state_options.do_calib_camera_pose &&
        !params.state_options.do_calib_camera_intrinsics &&
        !params.state_options.do_calib_camera_timeoffset &&
        params.state_options.max_slam_features >=
            params.long_gap_recovery_min_correspondences &&
        params.camera_intrinsics.size() == 2U &&
        std::dynamic_pointer_cast<CamRadtan>(
            params.camera_intrinsics.at(0)) != nullptr &&
        std::dynamic_pointer_cast<CamRadtan>(
            params.camera_intrinsics.at(1)) != nullptr &&
        params.long_gap_recovery_threshold == 0.5 &&
        params.long_gap_recovery_max_attempts == 10 &&
        params.long_gap_recovery_min_correspondences ==
            static_cast<int>(LongGapRelocalizer::minimum_correspondences()) &&
        params.long_gap_recovery_min_inliers ==
            static_cast<int>(LongGapRelocalizer::minimum_inliers()) &&
        params.long_gap_recovery_min_inlier_ratio ==
            LongGapRelocalizer::minimum_inlier_ratio() &&
        params.long_gap_recovery_reprojection_px ==
            LongGapRelocalizer::maximum_reprojection_error_px() &&
        params.long_gap_recovery_min_image_span_ratio ==
            LongGapRelocalizer::minimum_image_span_fraction() &&
        params.long_gap_recovery_max_imu_angle_deg ==
            LongGapRelocalizer::maximum_orientation_disagreement_deg() &&
        params.long_gap_recovery_consensus_frames == 3 &&
        params.long_gap_recovery_stationary_radius_m == 0.10 &&
        params.long_gap_recovery_orientation_sigma_deg == 2.0 &&
        params.long_gap_recovery_position_sigma_m == 0.10 &&
        params.long_gap_recovery_velocity_sigma_mps == 0.05;
    if (!frozen_recovery_contract) {
      throw std::runtime_error(
          "enabled long-gap recovery violates its frozen runtime contract");
    }
    PRINT_INFO(
        "[LONG-GAP-RECOVERY]: event=contract_validated enabled=1 "
        "threshold_s=0.500 attempts=10 consensus=3\n");
  }

  // This will globally set the thread count we will use
  // -1 will reset to the system default threading (usually the num of cores)
  cv::setNumThreads(params.num_opencv_threads);
  cv::setRNGSeed(0);

  // Create the state!!
  state = std::make_shared<State>(params.state_options);

  // Set the IMU intrinsics
  state->_calib_imu_dw->set_value(params.vec_dw);
  state->_calib_imu_dw->set_fej(params.vec_dw);
  state->_calib_imu_da->set_value(params.vec_da);
  state->_calib_imu_da->set_fej(params.vec_da);
  state->_calib_imu_tg->set_value(params.vec_tg);
  state->_calib_imu_tg->set_fej(params.vec_tg);
  state->_calib_imu_GYROtoIMU->set_value(params.q_GYROtoIMU);
  state->_calib_imu_GYROtoIMU->set_fej(params.q_GYROtoIMU);
  state->_calib_imu_ACCtoIMU->set_value(params.q_ACCtoIMU);
  state->_calib_imu_ACCtoIMU->set_fej(params.q_ACCtoIMU);

  // Timeoffset from camera to IMU
  Eigen::VectorXd temp_camimu_dt;
  temp_camimu_dt.resize(1);
  temp_camimu_dt(0) = params.calib_camimu_dt;
  state->_calib_dt_CAMtoIMU->set_value(temp_camimu_dt);
  state->_calib_dt_CAMtoIMU->set_fej(temp_camimu_dt);

  // Loop through and load each of the cameras
  state->_cam_intrinsics_cameras = params.camera_intrinsics;
  for (int i = 0; i < state->_options.num_cameras; i++) {
    state->_cam_intrinsics.at(i)->set_value(params.camera_intrinsics.at(i)->get_value());
    state->_cam_intrinsics.at(i)->set_fej(params.camera_intrinsics.at(i)->get_value());
    state->_calib_IMUtoCAM.at(i)->set_value(params.camera_extrinsics.at(i));
    state->_calib_IMUtoCAM.at(i)->set_fej(params.camera_extrinsics.at(i));
  }

  //===================================================================================
  //===================================================================================
  //===================================================================================

  // If we are recording statistics, then open our file
  if (params.record_timing_information) {
    if (params.cp2_preopened_output_mode) {
      if (!OpenCP2PreopenedLegacyStream(
              params.cp2_legacy_timing_canonical_path,
              params.record_timing_filepath,
              params.cp2_legacy_timing_capability, of_statistics)) {
        throw std::runtime_error(
            "CP2 preopened timing output could not be bound");
      }
    } else {
      // Ordinary OpenVINS behavior owns the pathname and may replace it.
      if (boost::filesystem::exists(params.record_timing_filepath)) {
        boost::filesystem::remove(params.record_timing_filepath);
        PRINT_INFO(YELLOW "[STATS]: found old file found, deleted...\n" RESET);
      }
      boost::filesystem::path p(params.record_timing_filepath);
      boost::filesystem::create_directories(p.parent_path());
      of_statistics.open(params.record_timing_filepath,
                         std::ofstream::out | std::ofstream::app);
    }
    if (!of_statistics.is_open() || !of_statistics.good()) {
      throw std::runtime_error("timing output stream failed to open");
    }
    // Write the header information into it
    of_statistics << "# timestamp (sec),tracking,propagation,msckf update,";
    if (state->_options.max_slam_features > 0) {
      of_statistics << "slam update,slam delayed,";
    }
    of_statistics << "re-tri & marg,total" << std::endl;
    if (!of_statistics.good()) {
      throw std::runtime_error("timing output header write failed");
    }
  }

  //===================================================================================
  //===================================================================================
  //===================================================================================

  // Let's make a feature extractor
  // NOTE: after we initialize we will increase the total number of feature tracks
  // NOTE: we will split the total number of features over all cameras uniformly
  int init_max_features = std::floor((double)params.init_options.init_max_features / (double)params.state_options.num_cameras);
  if (params.use_klt) {
    trackFEATS = std::shared_ptr<TrackBase>(new TrackKLT(state->_cam_intrinsics_cameras, init_max_features,
                                                         state->_options.max_aruco_features, params.use_stereo, params.histogram_method,
                                                         params.fast_threshold, params.grid_x, params.grid_y, params.min_px_dist));
  } else {
    trackFEATS = std::shared_ptr<TrackBase>(new TrackDescriptor(
        state->_cam_intrinsics_cameras, init_max_features, state->_options.max_aruco_features, params.use_stereo, params.histogram_method,
        params.fast_threshold, params.grid_x, params.grid_y, params.min_px_dist, params.knn_ratio));
  }

  // Initialize our aruco tag extractor
  if (params.use_aruco) {
    trackARUCO = std::shared_ptr<TrackBase>(new TrackAruco(state->_cam_intrinsics_cameras, state->_options.max_aruco_features,
                                                           params.use_stereo, params.histogram_method, params.downsize_aruco));
  }

  // Initialize our state propagator
  propagator = std::make_shared<Propagator>(params.imu_noises, params.gravity_mag);

  // Our state initialize
  initializer = std::make_shared<ov_init::InertialInitializer>(params.init_options, trackFEATS->get_feature_database());

  // Make the updater!
  updaterMSCKF = std::make_shared<UpdaterMSCKF>(params.msckf_options, params.featinit_options);
  updaterSLAM = std::make_shared<UpdaterSLAM>(params.slam_options, params.aruco_options, params.featinit_options);

  // Bind support claims to the fully resolved runtime objects and options.
  // The capture sink receives the result; it never accepts a caller-supplied
  // supported_configuration Boolean.
  if (params.turnsafe_t0.capture_requested &&
      !params.turnsafe_t0.output_path.empty()) {
    TurnSafeResolvedConfiguration turnsafe_configuration;
    turnsafe_configuration.one_pass_schur =
        params.msckf_options.landmark_elimination ==
            UpdaterOptions::LandmarkElimination::SCHUR &&
        params.msckf_options.max_visual_passes == 1;
    turnsafe_configuration.fej_enabled = params.state_options.do_fej;
    turnsafe_configuration.global_3d_transient =
        params.state_options.feat_rep_msckf ==
            ov_type::LandmarkRepresentation::Representation::GLOBAL_3D;
    turnsafe_configuration.camera_count =
        params.state_options.num_cameras > 0
            ? static_cast<std::uint64_t>(params.state_options.num_cameras)
            : 0U;
    turnsafe_configuration.all_cameras_radtan =
        params.camera_intrinsics.size() ==
            turnsafe_configuration.camera_count;
    for (const auto &camera : params.camera_intrinsics) {
      turnsafe_configuration.all_cameras_radtan =
          turnsafe_configuration.all_cameras_radtan &&
          std::dynamic_pointer_cast<CamRadtan>(camera.second) != nullptr;
    }
    turnsafe_configuration.camera_extrinsic_calibration_off =
        !params.state_options.do_calib_camera_pose;
    turnsafe_configuration.camera_intrinsic_calibration_off =
        !params.state_options.do_calib_camera_intrinsics;
    turnsafe_configuration.camera_time_offset_calibration_off =
        !params.state_options.do_calib_camera_timeoffset;
    turnsafe_configuration.stereo_enabled = params.use_stereo;
    turnsafe_configuration.stereo_available =
        params.use_stereo && turnsafe_configuration.camera_count == 2U &&
        params.camera_intrinsics.size() == 2U;
    turnsafe_configuration.require_target_stereo_range =
        params.turnsafe_t0.require_target_stereo_range;
    params.turnsafe_t0.resolved_configuration =
        std::move(turnsafe_configuration);
  }

  // Session-1 diagnostics are wholly absent unless both the opt-in bit and an
  // explicit output pathname are supplied. Weak callbacks prevent ownership
  // cycles and expose only value records to the sink.
  turnsafe_t0_diagnostics = TurnSafeDiagnostics::Create(params.turnsafe_t0);
  if (turnsafe_t0_diagnostics && turnsafe_t0_diagnostics->active()) {
    const std::shared_ptr<TrackKLT> klt =
        std::dynamic_pointer_cast<TrackKLT>(trackFEATS);
    const TurnSafeCallbackInstallationResult installation =
        install_turnsafe_t0_callbacks(turnsafe_t0_diagnostics, klt.get(),
                                      updaterMSCKF.get());
    if (!installation.success) {
      PRINT_WARNING(YELLOW
                    "[TURNSAFE-T0]: status=installation_rejected "
                    "baseline_unchanged=1\n" RESET);
    }
  } else if ((params.turnsafe_t0.capture_requested &&
              !params.turnsafe_t0.output_path.empty()) ||
             params.turnsafe_t0.capture_causal_imu_intervals ||
             params.turnsafe_t0.capture_outcome_association_keys ||
             params.turnsafe_t0.capture_group_bearing_provenance) {
    PRINT_WARNING(YELLOW
                  "[TURNSAFE-T0]: status=disabled reason=%s "
                  "baseline_unchanged=1\n" RESET,
                  turnsafe_t0_diagnostics
                      ? turnsafe_t0_diagnostics->failure_reason()
                      : "SINK_ALLOCATION_FAILED");
  }

  // If we are using zero velocity updates, then create the updater
  if (params.try_zupt) {
    updaterZUPT = std::make_shared<UpdaterZeroVelocity>(params.zupt_options, params.imu_noises, trackFEATS->get_feature_database(),
                                                        propagator, params.gravity_mag, params.zupt_max_velocity,
                                                        params.zupt_noise_multiplier, params.zupt_max_disparity);
  }
}

VioManager::~VioManager() { finalize_turnsafe_t0(); }

bool VioManager::finalize_turnsafe_t0() noexcept {
  return !turnsafe_t0_diagnostics || turnsafe_t0_diagnostics->Finalize();
}

bool VioManager::set_cp2_invocation_context(
    const CP2UpdateInvocationContext &context) noexcept {
  return !cp2_camera_context_active && updaterMSCKF &&
         updaterMSCKF->set_cp2_invocation_context(context);
}

bool VioManager::feed_measurement_camera_with_cp2_context(
    const ov_core::CameraData &message,
    const CP2UpdateInvocationContext &context, bool &updater_invoked) {
  updater_invoked = false;
  if (cp2_camera_context_active || !updaterMSCKF ||
      updaterMSCKF->cp2_trace_fatal_latched()) {
    return false;
  }
  cp2_camera_context = context;
  cp2_camera_context_consumed = false;
  cp2_camera_context_active = true;
  try {
    track_image_and_update(message);
    updater_invoked = cp2_camera_context_consumed;
    cp2_camera_context = CP2UpdateInvocationContext{};
    cp2_camera_context_consumed = false;
    cp2_camera_context_active = false;
    return true;
  } catch (...) {
    updater_invoked = cp2_camera_context_consumed;
    cp2_camera_context = CP2UpdateInvocationContext{};
    cp2_camera_context_consumed = false;
    cp2_camera_context_active = false;
    throw;
  }
}

bool VioManager::set_cp2_update_callback(
    UpdaterMSCKF::CP2UpdateCallback callback, bool enable_shadow) {
  return updaterMSCKF && updaterMSCKF->set_cp2_update_callback(
                             std::move(callback), enable_shadow);
}

bool VioManager::set_cp2_recorded_sink(
    std::shared_ptr<CP2RecordedUpdateSink> sink) {
  return updaterMSCKF &&
         updaterMSCKF->set_cp2_recorded_sink(std::move(sink));
}

bool VioManager::cp2_trace_fatal_latched() const noexcept {
  return updaterMSCKF && updaterMSCKF->cp2_trace_fatal_latched();
}

CP2TraceFatalReason VioManager::cp2_trace_fatal_reason() const noexcept {
  return updaterMSCKF ? updaterMSCKF->cp2_trace_fatal_reason()
                      : CP2TraceFatalReason::kNone;
}

void VioManager::feed_measurement_imu(const ov_core::ImuData &message) {

  // The oldest time we need IMU with is the last clone
  // We shouldn't really need the whole window, but if we go backwards in time we will
  double oldest_time = state->margtimestep();
  if (oldest_time > state->_timestamp) {
    oldest_time = -1;
  }
  if (!is_initialized_vio) {
    oldest_time = message.timestamp - params.init_options.init_window_time + state->_calib_dt_CAMtoIMU->value()(0) - 0.10;
  }
  propagator->feed_imu(message, oldest_time);

  // Push back to our initializer
  if (!is_initialized_vio) {
    initializer->feed_imu(message, oldest_time);
  }

  // Push back to the zero velocity updater if it is enabled
  // No need to push back if we are just doing the zv-update at the begining and we have moved
  if (is_initialized_vio && updaterZUPT != nullptr && (!params.zupt_only_at_beginning || !has_moved_since_zupt)) {
    updaterZUPT->feed_imu(message, oldest_time);
  }
}

void VioManager::feed_measurement_simulation(double timestamp, const std::vector<int> &camids,
                                             const std::vector<std::vector<std::pair<size_t, Eigen::VectorXf>>> &feats) {

  // Start timing
  rT1 = boost::posix_time::microsec_clock::local_time();

  // Check if we actually have a simulated tracker
  // If not, recreate and re-cast the tracker to our simulation tracker
  std::shared_ptr<TrackSIM> trackSIM = std::dynamic_pointer_cast<TrackSIM>(trackFEATS);
  if (trackSIM == nullptr) {
    // Replace with the simulated tracker
    trackSIM = std::make_shared<TrackSIM>(state->_cam_intrinsics_cameras, state->_options.max_aruco_features);
    trackFEATS = trackSIM;
    // Need to also replace it in init and zv-upt since it points to the trackFEATS db pointer
    initializer = std::make_shared<ov_init::InertialInitializer>(params.init_options, trackFEATS->get_feature_database());
    if (params.try_zupt) {
      updaterZUPT = std::make_shared<UpdaterZeroVelocity>(params.zupt_options, params.imu_noises, trackFEATS->get_feature_database(),
                                                          propagator, params.gravity_mag, params.zupt_max_velocity,
                                                          params.zupt_noise_multiplier, params.zupt_max_disparity);
    }
    PRINT_WARNING(RED "[SIM]: casting our tracker to a TrackSIM object!\n" RESET);
  }

  // Feed our simulation tracker
  trackSIM->feed_measurement_simulation(timestamp, camids, feats);
  rT2 = boost::posix_time::microsec_clock::local_time();

  // Check if we should do zero-velocity, if so update the state with it
  // Note that in the case that we only use in the beginning initialization phase
  // If we have since moved, then we should never try to do a zero velocity update!
  if (is_initialized_vio && updaterZUPT != nullptr && (!params.zupt_only_at_beginning || !has_moved_since_zupt)) {
    // If the same state time, use the previous timestep decision
    if (state->_timestamp != timestamp) {
      did_zupt_update = updaterZUPT->try_update(state, timestamp);
    }
    if (did_zupt_update) {
      assert(state->_timestamp == timestamp);
      propagator->clean_old_imu_measurements(timestamp + state->_calib_dt_CAMtoIMU->value()(0) - 0.10);
      updaterZUPT->clean_old_imu_measurements(timestamp + state->_calib_dt_CAMtoIMU->value()(0) - 0.10);
      propagator->invalidate_cache();
      return;
    }
  }

  // If we do not have VIO initialization, then return an error
  if (!is_initialized_vio) {
    PRINT_ERROR(RED "[SIM]: your vio system should already be initialized before simulating features!!!\n" RESET);
    PRINT_ERROR(RED "[SIM]: initialize your system first before calling feed_measurement_simulation()!!!!\n" RESET);
    std::exit(EXIT_FAILURE);
  }

  // Call on our propagate and update function
  // Simulation is either all sync, or single camera...
  ov_core::CameraData message;
  message.timestamp = timestamp;
  for (auto const &camid : camids) {
    int width = state->_cam_intrinsics_cameras.at(camid)->w();
    int height = state->_cam_intrinsics_cameras.at(camid)->h();
    message.sensor_ids.push_back(camid);
    message.images.push_back(cv::Mat::zeros(cv::Size(width, height), CV_8UC1));
    message.masks.push_back(cv::Mat::zeros(cv::Size(width, height), CV_8UC1));
  }
  do_feature_propagate_update(message);
}

void VioManager::track_image_and_update(const ov_core::CameraData &message_const) {

  // Start timing
  rT1 = boost::posix_time::microsec_clock::local_time();
  const TurnSafeCallbackEnvelope turnsafe_callback_envelope(
      turnsafe_t0_diagnostics,
      std::dynamic_pointer_cast<const TrackKLT>(trackFEATS), updaterMSCKF,
      message_const.timestamp, message_const.sensor_ids, &state,
      propagator.get(), &is_initialized_vio, &timelastupdate);

  // Assert we have valid measurement data and ids
  assert(!message_const.sensor_ids.empty());
  assert(message_const.sensor_ids.size() == message_const.images.size());
  for (size_t i = 0; i < message_const.sensor_ids.size() - 1; i++) {
    assert(message_const.sensor_ids.at(i) != message_const.sensor_ids.at(i + 1));
  }

  // Downsample if we are downsampling
  ov_core::CameraData message = message_const;
  for (size_t i = 0; i < message.sensor_ids.size() && params.downsample_cameras; i++) {
    cv::Mat img = message.images.at(i);
    cv::Mat mask = message.masks.at(i);
    cv::Mat img_temp, mask_temp;
    cv::pyrDown(img, img_temp, cv::Size(img.cols / 2.0, img.rows / 2.0));
    message.images.at(i) = img_temp;
    cv::pyrDown(mask, mask_temp, cv::Size(mask.cols / 2.0, mask.rows / 2.0));
    message.masks.at(i) = mask_temp;
  }

  // Detect an initialized camera outage before the ordinary frontend/state
  // pipeline can propagate the stale position and velocity through it.
  if (params.long_gap_recovery_enabled && is_initialized_vio && state &&
      long_gap_recovery_phase == LongGapRecoveryPhase::TRACKING &&
      std::isfinite(state->_timestamp) &&
      message.timestamp - state->_timestamp >
          params.long_gap_recovery_threshold) {
    long_gap_recovery_phase = LongGapRecoveryPhase::REACQUIRING;
    long_gap_recovery_attempt_count = 0;
    long_gap_recovery_consensus.clear();
    ++long_gap_recovery_activation_count;
    PRINT_INFO(
        "[LONG-GAP-RECOVERY]: event=trigger epoch=%llu timestamp=%.15f "
        "last_state_timestamp=%.15f gap_s=%.9f activation=%llu\n",
        static_cast<unsigned long long>(epoch_id), message.timestamp,
        state->_timestamp, message.timestamp - state->_timestamp,
        static_cast<unsigned long long>(long_gap_recovery_activation_count));
  }

  // Perform our feature tracking!
  trackFEATS->feed_new_camera(message);

  // If the aruco tracker is available, the also pass to it
  // NOTE: binocular tracking for aruco doesn't make sense as we by default have the ids
  // NOTE: thus we just call the stereo tracking if we are doing binocular!
  if (is_initialized_vio && trackARUCO != nullptr) {
    trackARUCO->feed_new_camera(message);
  }
  rT2 = boost::posix_time::microsec_clock::local_time();

  // Reacquisition frames may update only the temporary frontend.  They never
  // enter ZUPT, initialization, propagation, or either visual updater while
  // the old live state is stale.  FAILED remains fail-closed for the rest of
  // the sequence and preserves all diagnostic prefixes.
  if (params.long_gap_recovery_enabled &&
      (long_gap_recovery_phase == LongGapRecoveryPhase::REACQUIRING ||
       long_gap_recovery_phase == LongGapRecoveryPhase::FAILED)) {
    process_long_gap_recovery_frame(message);
    return;
  }

  // Check if we should do zero-velocity, if so update the state with it
  // Note that in the case that we only use in the beginning initialization phase
  // If we have since moved, then we should never try to do a zero velocity update!
  if (is_initialized_vio && updaterZUPT != nullptr && (!params.zupt_only_at_beginning || !has_moved_since_zupt)) {
    // If the same state time, use the previous timestep decision
    if (state->_timestamp != message.timestamp) {
      did_zupt_update = updaterZUPT->try_update(state, message.timestamp);
    }
    if (did_zupt_update) {
      assert(state->_timestamp == message.timestamp);
      propagator->clean_old_imu_measurements(message.timestamp + state->_calib_dt_CAMtoIMU->value()(0) - 0.10);
      updaterZUPT->clean_old_imu_measurements(message.timestamp + state->_calib_dt_CAMtoIMU->value()(0) - 0.10);
      propagator->invalidate_cache();
      return;
    }
  }

  // If we do not have VIO initialization, then try to initialize
  // TODO: Or if we are trying to reset the system, then do that here!
  if (!is_initialized_vio) {
    is_initialized_vio = try_to_initialize(message);
    if (!is_initialized_vio) {
      double time_track = (rT2 - rT1).total_microseconds() * 1e-6;
      PRINT_DEBUG(BLUE "[TIME]: %.4f seconds for tracking\n" RESET, time_track);
      return;
    }
  }

  // Call on our propagate and update function
  do_feature_propagate_update(message);

  if (params.long_gap_recovery_enabled &&
      long_gap_recovery_phase == LongGapRecoveryPhase::WARMUP && state &&
      static_cast<int>(state->_clones_IMU.size()) >=
          std::min(state->_options.max_clone_size, 5) &&
      timelastupdate == message.timestamp) {
    long_gap_recovery_phase = LongGapRecoveryPhase::TRACKING;
    PRINT_INFO(
        "[LONG-GAP-RECOVERY]: event=warmup_complete epoch=%llu "
        "timestamp=%.15f clones=%zu\n",
        static_cast<unsigned long long>(epoch_id), message.timestamp,
        state->_clones_IMU.size());
  }
}

bool VioManager::process_long_gap_recovery_frame(
    const ov_core::CameraData &message) {
  if (long_gap_recovery_phase == LongGapRecoveryPhase::FAILED) {
    PRINT_INFO(
        "[LONG-GAP-RECOVERY]: event=degraded_frame epoch=%llu "
        "timestamp=%.15f state_unchanged=1\n",
        static_cast<unsigned long long>(epoch_id), message.timestamp);
    return true;
  }
  if (long_gap_recovery_phase != LongGapRecoveryPhase::REACQUIRING ||
      !state || !trackFEATS || !propagator) {
    return false;
  }

  ++long_gap_recovery_attempt_count;

  LongGapCameraCalibration calibration;
  const Eigen::VectorXd intrinsics = state->_cam_intrinsics.at(0)->value();
  if (intrinsics.rows() >= 8) {
    calibration.K << intrinsics(0), 0.0, intrinsics(2), 0.0,
        intrinsics(1), intrinsics(3), 0.0, 0.0, 1.0;
    calibration.distortion = {intrinsics(4), intrinsics(5), intrinsics(6),
                              intrinsics(7)};
  } else {
    calibration.K.setConstant(
        std::numeric_limits<double>::quiet_NaN());
  }
  calibration.image_width = state->_cam_intrinsics_cameras.at(0)->w();
  calibration.image_height = state->_cam_intrinsics_cameras.at(0)->h();
  calibration.R_ItoC = state->_calib_IMUtoCAM.at(0)->Rot();
  calibration.p_IinC = state->_calib_IMUtoCAM.at(0)->pos();

  const auto last_observations = trackFEATS->get_last_obs();
  const auto last_ids = trackFEATS->get_last_ids();
  std::vector<LongGapCorrespondence> correspondences;
  const auto observations_cam0 = last_observations.find(0U);
  const auto ids_cam0 = last_ids.find(0U);
  if (observations_cam0 != last_observations.end() &&
      ids_cam0 != last_ids.end() &&
      observations_cam0->second.size() == ids_cam0->second.size()) {
    correspondences.reserve(ids_cam0->second.size());
    for (std::size_t index = 0U; index < ids_cam0->second.size(); ++index) {
      const std::size_t feature_id = ids_cam0->second[index];
      const auto retained = state->_features_SLAM.find(feature_id);
      if (retained == state->_features_SLAM.end() || !retained->second ||
          feature_id <= static_cast<std::size_t>(
                            4 * state->_options.max_aruco_features)) {
        continue;
      }

      Eigen::Vector3d p_FinG = retained->second->get_xyz(false);
      if (LandmarkRepresentation::is_relative_representation(
              retained->second->_feat_representation)) {
        const int anchor_camera = retained->second->_anchor_cam_id;
        const auto anchor = state->_clones_IMU.find(
            retained->second->_anchor_clone_timestamp);
        if (anchor_camera < 0 ||
            anchor_camera >= state->_options.num_cameras ||
            anchor == state->_clones_IMU.end() || !anchor->second) {
          continue;
        }
        const Eigen::Matrix3d R_ItoC_anchor =
            state->_calib_IMUtoCAM.at(anchor_camera)->Rot();
        const Eigen::Vector3d p_IinC_anchor =
            state->_calib_IMUtoCAM.at(anchor_camera)->pos();
        p_FinG = anchor->second->Rot().transpose() *
                      R_ItoC_anchor.transpose() *
                      (p_FinG - p_IinC_anchor) +
                  anchor->second->pos();
      }

      LongGapCorrespondence correspondence;
      correspondence.id = static_cast<std::uint64_t>(feature_id);
      correspondence.p_FinG = p_FinG;
      correspondence.uv_raw <<
          static_cast<double>(observations_cam0->second[index].pt.x),
          static_cast<double>(observations_cam0->second[index].pt.y);
      correspondences.push_back(correspondence);
    }
  }
  std::sort(correspondences.begin(), correspondences.end(),
            [](const LongGapCorrespondence &left,
               const LongGapCorrespondence &right) {
              return left.id < right.id;
            });

  Eigen::Matrix3d imu_predicted_R_GtoI = Eigen::Matrix3d::Constant(
      std::numeric_limits<double>::quiet_NaN());
  const bool imu_prediction_available =
      propagator->predict_orientation_readonly(
          state, message.timestamp, imu_predicted_R_GtoI);
  const LongGapRelocalizationResult recovery = LongGapRelocalizer::recover(
      correspondences, calibration, imu_predicted_R_GtoI);
  const LongGapRelocalizationMetrics &metrics = recovery.metrics;
  PRINT_INFO(
      "[LONG-GAP-RECOVERY]: event=attempt epoch=%llu attempt=%d "
      "timestamp=%.15f imu_prediction=%d accepted=%d reason=%s "
      "supplied=%zu valid=%zu inliers=%zu inlier_ratio=%.9f "
      "max_reprojection_px=%.9f min_depth=%.9f span_x=%.9f "
      "span_y=%.9f support_ratio=%.9f imu_angle_deg=%.9f "
      "state_unchanged=%d\n",
      static_cast<unsigned long long>(epoch_id),
      long_gap_recovery_attempt_count, message.timestamp,
      static_cast<int>(imu_prediction_available),
      static_cast<int>(recovery.accepted),
      long_gap_relocalization_reason_string(recovery.reason),
      metrics.supplied_correspondence_count,
      metrics.valid_correspondence_count, metrics.inlier_count,
      metrics.inlier_ratio, metrics.max_reprojection_error_px,
      metrics.min_depth, metrics.image_span_x_fraction,
      metrics.image_span_y_fraction,
      metrics.second_to_first_3d_singular_ratio,
      metrics.orientation_disagreement_deg,
      static_cast<int>(!recovery.accepted));

  if (!recovery.accepted) {
    long_gap_recovery_consensus.clear();
  } else {
    LongGapRecoveryPoseSample sample;
    sample.timestamp = message.timestamp;
    sample.R_GtoI = recovery.R_GtoI;
    sample.p_IinG = recovery.p_IinG;
    long_gap_recovery_consensus.push_back(sample);
    PRINT_INFO(
        "[LONG-GAP-RECOVERY]: event=accepted_pose epoch=%llu attempt=%d "
        "timestamp=%.15f p_x=%.9f p_y=%.9f p_z=%.9f "
        "consecutive=%zu\n",
        static_cast<unsigned long long>(epoch_id),
        long_gap_recovery_attempt_count, message.timestamp,
        recovery.p_IinG(0), recovery.p_IinG(1), recovery.p_IinG(2),
        long_gap_recovery_consensus.size());
  }

  if (static_cast<int>(long_gap_recovery_consensus.size()) >=
      params.long_gap_recovery_consensus_frames) {
    Eigen::Vector3d component_median;
    for (int dimension = 0; dimension < 3; ++dimension) {
      std::vector<double> values;
      values.reserve(long_gap_recovery_consensus.size());
      for (const auto &sample : long_gap_recovery_consensus) {
        values.push_back(sample.p_IinG(dimension));
      }
      std::sort(values.begin(), values.end());
      component_median(dimension) = values[values.size() / 2U];
    }
    double maximum_radius = 0.0;
    for (const auto &sample : long_gap_recovery_consensus) {
      maximum_radius = std::max(
          maximum_radius, (sample.p_IinG - component_median).norm());
    }
    if (std::isfinite(maximum_radius) &&
        maximum_radius <=
            params.long_gap_recovery_stationary_radius_m) {
      const LongGapRecoveryPoseSample verified =
          long_gap_recovery_consensus.back();
      PRINT_INFO(
          "[LONG-GAP-RECOVERY]: event=consensus_pass epoch=%llu "
          "timestamp=%.15f radius_m=%.9f samples=%zu\n",
          static_cast<unsigned long long>(epoch_id), verified.timestamp,
          maximum_radius, long_gap_recovery_consensus.size());
      commit_long_gap_recovery(message, verified.R_GtoI,
                               verified.p_IinG);
      return true;
    }

    PRINT_INFO(
        "[LONG-GAP-RECOVERY]: event=consensus_reject epoch=%llu "
        "timestamp=%.15f radius_m=%.9f threshold_m=%.9f\n",
        static_cast<unsigned long long>(epoch_id), message.timestamp,
        maximum_radius, params.long_gap_recovery_stationary_radius_m);
    long_gap_recovery_consensus.clear();
  }

  if (long_gap_recovery_attempt_count >=
      params.long_gap_recovery_max_attempts) {
    long_gap_recovery_phase = LongGapRecoveryPhase::FAILED;
    ++long_gap_recovery_failure_count;
    long_gap_recovery_consensus.clear();
    PRINT_INFO(
        "[LONG-GAP-RECOVERY]: event=recovery_failed epoch=%llu "
        "timestamp=%.15f attempts=%d state_unchanged=1\n",
        static_cast<unsigned long long>(epoch_id), message.timestamp,
        long_gap_recovery_attempt_count);
  }
  return true;
}

void VioManager::do_feature_propagate_update(const ov_core::CameraData &message) {

  //===================================================================================
  // State propagation, and clone augmentation
  //===================================================================================

  // Return if the camera measurement is out of order
  if (state->_timestamp > message.timestamp) {
    PRINT_WARNING(YELLOW "image received out of order, unable to do anything (prop dt = %3f)\n" RESET,
                  (message.timestamp - state->_timestamp));
    return;
  }

  // Propagate the state forward to the current update time
  // Also augment it with a new clone!
  // NOTE: if the state is already at the given time (can happen in sim)
  // NOTE: then no need to prop since we already are at the desired timestep
  if (state->_timestamp != message.timestamp) {
    propagator->propagate_and_clone(state, message.timestamp);
  }
  rT3 = boost::posix_time::microsec_clock::local_time();

  // If we have not reached max clones, we should just return...
  // This isn't super ideal, but it keeps the logic after this easier...
  // We can start processing things when we have at least 5 clones since we can start triangulating things...
  if ((int)state->_clones_IMU.size() < std::min(state->_options.max_clone_size, 5)) {
    PRINT_DEBUG("waiting for enough clone states (%d of %d)....\n", (int)state->_clones_IMU.size(),
                std::min(state->_options.max_clone_size, 5));
    return;
  }

  // Return if we where unable to propagate
  if (state->_timestamp != message.timestamp) {
    PRINT_WARNING(RED "[PROP]: Propagator unable to propagate the state forward in time!\n" RESET);
    PRINT_WARNING(RED "[PROP]: It has been %.3f since last time we propagated\n" RESET, message.timestamp - state->_timestamp);
    return;
  }
  has_moved_since_zupt = true;

  //===================================================================================
  // MSCKF features and KLT tracks that are SLAM features
  //===================================================================================

  // Now, lets get all features that should be used for an update that are lost in the newest frame
  // We explicitly request features that have not been deleted (used) in another update step
  std::vector<std::shared_ptr<Feature>> feats_lost, feats_marg, feats_slam;
  feats_lost = trackFEATS->get_feature_database()->features_not_containing_newer(state->_timestamp, false, true);

  // Don't need to get the oldest features until we reach our max number of clones
  if ((int)state->_clones_IMU.size() > state->_options.max_clone_size || (int)state->_clones_IMU.size() > 5) {
    feats_marg = trackFEATS->get_feature_database()->features_containing(state->margtimestep(), false, true);
    if (trackARUCO != nullptr && message.timestamp - startup_time >= params.dt_slam_delay) {
      feats_slam = trackARUCO->get_feature_database()->features_containing(state->margtimestep(), false, true);
    }
  }

  // Remove any lost features that were from other image streams
  // E.g: if we are cam1 and cam0 has not processed yet, we don't want to try to use those in the update yet
  // E.g: thus we wait until cam0 process its newest image to remove features which were seen from that camera
  auto it1 = feats_lost.begin();
  while (it1 != feats_lost.end()) {
    bool found_current_message_camid = false;
    for (const auto &camuvpair : (*it1)->uvs) {
      if (std::find(message.sensor_ids.begin(), message.sensor_ids.end(), camuvpair.first) != message.sensor_ids.end()) {
        found_current_message_camid = true;
        break;
      }
    }
    if (found_current_message_camid) {
      it1++;
    } else {
      it1 = feats_lost.erase(it1);
    }
  }

  // We also need to make sure that the max tracks does not contain any lost features
  // This could happen if the feature was lost in the last frame, but has a measurement at the marg timestep
  it1 = feats_lost.begin();
  while (it1 != feats_lost.end()) {
    if (std::find(feats_marg.begin(), feats_marg.end(), (*it1)) != feats_marg.end()) {
      // PRINT_WARNING(YELLOW "FOUND FEATURE THAT WAS IN BOTH feats_lost and feats_marg!!!!!!\n" RESET);
      it1 = feats_lost.erase(it1);
    } else {
      it1++;
    }
  }

  // Find tracks that have reached max length, these can be made into SLAM features
  std::vector<std::shared_ptr<Feature>> feats_maxtracks;
  auto it2 = feats_marg.begin();
  while (it2 != feats_marg.end()) {
    // See if any of our camera's reached max track
    bool reached_max = false;
    for (const auto &cams : (*it2)->timestamps) {
      if ((int)cams.second.size() > state->_options.max_clone_size) {
        reached_max = true;
        break;
      }
    }
    // If max track, then add it to our possible slam feature list
    if (reached_max) {
      feats_maxtracks.push_back(*it2);
      it2 = feats_marg.erase(it2);
    } else {
      it2++;
    }
  }

  // Count how many aruco tags we have in our state
  int curr_aruco_tags = 0;
  auto it0 = state->_features_SLAM.begin();
  while (it0 != state->_features_SLAM.end()) {
    if ((int)(*it0).second->_featid <= 4 * state->_options.max_aruco_features)
      curr_aruco_tags++;
    it0++;
  }

  // Append a new SLAM feature if we have the room to do so
  // Also check that we have waited our delay amount (normally prevents bad first set of slam points)
  if (state->_options.max_slam_features > 0 && message.timestamp - startup_time >= params.dt_slam_delay &&
      (int)state->_features_SLAM.size() < state->_options.max_slam_features + curr_aruco_tags) {
    // Get the total amount to add, then the max amount that we can add given our marginalize feature array
    int amount_to_add = (state->_options.max_slam_features + curr_aruco_tags) - (int)state->_features_SLAM.size();
    int valid_amount = (amount_to_add > (int)feats_maxtracks.size()) ? (int)feats_maxtracks.size() : amount_to_add;
    // If we have at least 1 that we can add, lets add it!
    // Note: we remove them from the feat_marg array since we don't want to reuse information...
    if (valid_amount > 0) {
      feats_slam.insert(feats_slam.end(), feats_maxtracks.end() - valid_amount, feats_maxtracks.end());
      feats_maxtracks.erase(feats_maxtracks.end() - valid_amount, feats_maxtracks.end());
    }
  }

  // Loop through current SLAM features, we have tracks of them, grab them for this update!
  // NOTE: if we have a slam feature that has lost tracking, then we should marginalize it out
  // NOTE: we only enforce this if the current camera message is where the feature was seen from
  // NOTE: if you do not use FEJ, these types of slam features *degrade* the estimator performance....
  // NOTE: we will also marginalize SLAM features if they have failed their update a couple times in a row
  for (std::pair<const size_t, std::shared_ptr<Landmark>> &landmark : state->_features_SLAM) {
    if (trackARUCO != nullptr) {
      std::shared_ptr<Feature> feat1 = trackARUCO->get_feature_database()->get_feature(landmark.second->_featid);
      if (feat1 != nullptr)
        feats_slam.push_back(feat1);
    }
    std::shared_ptr<Feature> feat2 = trackFEATS->get_feature_database()->get_feature(landmark.second->_featid);
    if (feat2 != nullptr)
      feats_slam.push_back(feat2);
    assert(landmark.second->_unique_camera_id != -1);
    bool current_unique_cam =
        std::find(message.sensor_ids.begin(), message.sensor_ids.end(), landmark.second->_unique_camera_id) != message.sensor_ids.end();
    if (feat2 == nullptr && current_unique_cam)
      landmark.second->should_marg = true;
    if (landmark.second->update_fail_count > 1)
      landmark.second->should_marg = true;
  }

  // Lets marginalize out all old SLAM features here
  // These are ones that where not successfully tracked into the current frame
  // We do *NOT* marginalize out our aruco tags landmarks
  StateHelper::marginalize_slam(state);

  // Separate our SLAM features into new ones, and old ones
  std::vector<std::shared_ptr<Feature>> feats_slam_DELAYED, feats_slam_UPDATE;
  for (size_t i = 0; i < feats_slam.size(); i++) {
    if (state->_features_SLAM.find(feats_slam.at(i)->featid) != state->_features_SLAM.end()) {
      feats_slam_UPDATE.push_back(feats_slam.at(i));
      // PRINT_DEBUG("[UPDATE-SLAM]: found old feature %d (%d
      // measurements)\n",(int)feats_slam.at(i)->featid,(int)feats_slam.at(i)->timestamps_left.size());
    } else {
      feats_slam_DELAYED.push_back(feats_slam.at(i));
      // PRINT_DEBUG("[UPDATE-SLAM]: new feature ready %d (%d
      // measurements)\n",(int)feats_slam.at(i)->featid,(int)feats_slam.at(i)->timestamps_left.size());
    }
  }

  // Concatenate our MSCKF feature arrays (i.e., ones not being used for slam updates)
  std::vector<std::shared_ptr<Feature>> featsup_MSCKF = feats_lost;
  featsup_MSCKF.insert(featsup_MSCKF.end(), feats_marg.begin(), feats_marg.end());
  featsup_MSCKF.insert(featsup_MSCKF.end(), feats_maxtracks.begin(), feats_maxtracks.end());

  //===================================================================================
  // Now that we have a list of features, lets do the EKF update for MSCKF and SLAM!
  //===================================================================================

  // Sort based on track length
  // TODO: we should have better selection logic here (i.e. even feature distribution in the FOV etc..)
  // TODO: right now features that are "lost" are at the front of this vector, while ones at the end are long-tracks
  auto compare_feat = [](const std::shared_ptr<Feature> &a, const std::shared_ptr<Feature> &b) -> bool {
    size_t asize = 0;
    size_t bsize = 0;
    for (const auto &pair : a->timestamps)
      asize += pair.second.size();
    for (const auto &pair : b->timestamps)
      bsize += pair.second.size();
    return asize < bsize;
  };
  std::sort(featsup_MSCKF.begin(), featsup_MSCKF.end(), compare_feat);

  // Pass them to our MSCKF updater
  // NOTE: if we have more then the max, we select the "best" ones (i.e. max tracks) for this update
  // NOTE: this should only really be used if you want to track a lot of features, or have limited computational resources
  if ((int)featsup_MSCKF.size() > state->_options.max_msckf_in_update)
    featsup_MSCKF.erase(featsup_MSCKF.begin(), featsup_MSCKF.end() - state->_options.max_msckf_in_update);
  if (cp2_camera_context_active) {
    if (!updaterMSCKF->set_cp2_invocation_context(cp2_camera_context)) {
      throw std::logic_error(
          "CP2 serial camera identity was rejected at the MSCKF call boundary");
    }
    cp2_camera_context_consumed = true;
  }
  updaterMSCKF->update(state, featsup_MSCKF);
  propagator->invalidate_cache();
  rT4 = boost::posix_time::microsec_clock::local_time();

  // Perform SLAM delay init and update
  // NOTE: that we provide the option here to do a *sequential* update
  // NOTE: this will be a lot faster but won't be as accurate.
  std::vector<std::shared_ptr<Feature>> feats_slam_UPDATE_TEMP;
  while (!feats_slam_UPDATE.empty()) {
    // Get sub vector of the features we will update with
    std::vector<std::shared_ptr<Feature>> featsup_TEMP;
    featsup_TEMP.insert(featsup_TEMP.begin(), feats_slam_UPDATE.begin(),
                        feats_slam_UPDATE.begin() + std::min(state->_options.max_slam_in_update, (int)feats_slam_UPDATE.size()));
    feats_slam_UPDATE.erase(feats_slam_UPDATE.begin(),
                            feats_slam_UPDATE.begin() + std::min(state->_options.max_slam_in_update, (int)feats_slam_UPDATE.size()));
    // Do the update
    updaterSLAM->update(state, featsup_TEMP);
    feats_slam_UPDATE_TEMP.insert(feats_slam_UPDATE_TEMP.end(), featsup_TEMP.begin(), featsup_TEMP.end());
    propagator->invalidate_cache();
  }
  feats_slam_UPDATE = feats_slam_UPDATE_TEMP;
  rT5 = boost::posix_time::microsec_clock::local_time();
  updaterSLAM->delayed_init(state, feats_slam_DELAYED);
  rT6 = boost::posix_time::microsec_clock::local_time();

  //===================================================================================
  // Update our visualization feature set, and clean up the old features
  //===================================================================================

  // Re-triangulate all current tracks in the current frame
  if (message.sensor_ids.at(0) == 0) {

    // Re-triangulate features
    retriangulate_active_tracks(message);

    // Clear the MSCKF features only on the base camera
    // Thus we should be able to visualize the other unique camera stream
    // MSCKF features as they will also be appended to the vector
    good_features_MSCKF.clear();
  }

  // Save all the MSCKF features used in the update
  for (auto const &feat : featsup_MSCKF) {
    good_features_MSCKF.push_back(feat->p_FinG);
    feat->to_delete = true;
  }

  //===================================================================================
  // Cleanup, marginalize out what we don't need any more...
  //===================================================================================

  // Remove features that where used for the update from our extractors at the last timestep
  // This allows for measurements to be used in the future if they failed to be used this time
  // Note we need to do this before we feed a new image, as we want all new measurements to NOT be deleted
  trackFEATS->get_feature_database()->cleanup();
  if (trackARUCO != nullptr) {
    trackARUCO->get_feature_database()->cleanup();
  }

  // First do anchor change if we are about to lose an anchor pose
  updaterSLAM->change_anchors(state);

  // Cleanup any features older than the marginalization time
  if ((int)state->_clones_IMU.size() > state->_options.max_clone_size) {
    trackFEATS->get_feature_database()->cleanup_measurements(state->margtimestep());
    if (trackARUCO != nullptr) {
      trackARUCO->get_feature_database()->cleanup_measurements(state->margtimestep());
    }
  }

  // Finally marginalize the oldest clone if needed
  StateHelper::marginalize_old_clone(state);
  rT7 = boost::posix_time::microsec_clock::local_time();

  //===================================================================================
  // Debug info, and stats tracking
  //===================================================================================

  // Get timing statitics information
  double time_track = (rT2 - rT1).total_microseconds() * 1e-6;
  double time_prop = (rT3 - rT2).total_microseconds() * 1e-6;
  double time_msckf = (rT4 - rT3).total_microseconds() * 1e-6;
  double time_slam_update = (rT5 - rT4).total_microseconds() * 1e-6;
  double time_slam_delay = (rT6 - rT5).total_microseconds() * 1e-6;
  double time_marg = (rT7 - rT6).total_microseconds() * 1e-6;
  double time_total = (rT7 - rT1).total_microseconds() * 1e-6;

  // Timing information
  PRINT_DEBUG(BLUE "[TIME]: %.4f seconds for tracking\n" RESET, time_track);
  PRINT_DEBUG(BLUE "[TIME]: %.4f seconds for propagation\n" RESET, time_prop);
  PRINT_DEBUG(BLUE "[TIME]: %.4f seconds for MSCKF update (%d feats)\n" RESET, time_msckf, (int)featsup_MSCKF.size());
  if (state->_options.max_slam_features > 0) {
    PRINT_DEBUG(BLUE "[TIME]: %.4f seconds for SLAM update (%d feats)\n" RESET, time_slam_update, (int)state->_features_SLAM.size());
    PRINT_DEBUG(BLUE "[TIME]: %.4f seconds for SLAM delayed init (%d feats)\n" RESET, time_slam_delay, (int)feats_slam_DELAYED.size());
  }
  PRINT_DEBUG(BLUE "[TIME]: %.4f seconds for re-tri & marg (%d clones in state)\n" RESET, time_marg, (int)state->_clones_IMU.size());

  std::stringstream ss;
  ss << "[TIME]: " << std::setprecision(4) << time_total << " seconds for total (camera";
  for (const auto &id : message.sensor_ids) {
    ss << " " << id;
  }
  ss << ")" << std::endl;
  PRINT_DEBUG(BLUE "%s" RESET, ss.str().c_str());

  // Finally if we are saving stats to file, lets save it to file
  if (params.record_timing_information && of_statistics.is_open()) {
    // We want to publish in the IMU clock frame
    // The timestamp in the state will be the last camera time
    double t_ItoC = state->_calib_dt_CAMtoIMU->value()(0);
    double timestamp_inI = state->_timestamp + t_ItoC;
    // Append to the file
    of_statistics << std::fixed << std::setprecision(15) << timestamp_inI << "," << std::fixed << std::setprecision(5) << time_track << ","
                  << time_prop << "," << time_msckf << ",";
    if (state->_options.max_slam_features > 0) {
      of_statistics << time_slam_update << "," << time_slam_delay << ",";
    }
    of_statistics << time_marg << "," << time_total << std::endl;
    of_statistics.flush();
  }

  // Update our distance traveled
  if (timelastupdate != -1 && state->_clones_IMU.find(timelastupdate) != state->_clones_IMU.end()) {
    Eigen::Matrix<double, 3, 1> dx = state->_imu->pos() - state->_clones_IMU.at(timelastupdate)->pos();
    distance += dx.norm();
  }
  timelastupdate = message.timestamp;

  // Debug, print our current state
  PRINT_INFO("q_GtoI = %.3f,%.3f,%.3f,%.3f | p_IinG = %.3f,%.3f,%.3f | dist = %.2f (meters)\n", state->_imu->quat()(0),
             state->_imu->quat()(1), state->_imu->quat()(2), state->_imu->quat()(3), state->_imu->pos()(0), state->_imu->pos()(1),
             state->_imu->pos()(2), distance);
  PRINT_INFO("bg = %.4f,%.4f,%.4f | ba = %.4f,%.4f,%.4f\n", state->_imu->bias_g()(0), state->_imu->bias_g()(1), state->_imu->bias_g()(2),
             state->_imu->bias_a()(0), state->_imu->bias_a()(1), state->_imu->bias_a()(2));

  // Debug for camera imu offset
  if (state->_options.do_calib_camera_timeoffset) {
    PRINT_INFO("camera-imu timeoffset = %.5f\n", state->_calib_dt_CAMtoIMU->value()(0));
  }

  // Debug for camera intrinsics
  if (state->_options.do_calib_camera_intrinsics) {
    for (int i = 0; i < state->_options.num_cameras; i++) {
      std::shared_ptr<Vec> calib = state->_cam_intrinsics.at(i);
      PRINT_INFO("cam%d intrinsics = %.3f,%.3f,%.3f,%.3f | %.3f,%.3f,%.3f,%.3f\n", (int)i, calib->value()(0), calib->value()(1),
                 calib->value()(2), calib->value()(3), calib->value()(4), calib->value()(5), calib->value()(6), calib->value()(7));
    }
  }

  // Debug for camera extrinsics
  if (state->_options.do_calib_camera_pose) {
    for (int i = 0; i < state->_options.num_cameras; i++) {
      std::shared_ptr<PoseJPL> calib = state->_calib_IMUtoCAM.at(i);
      PRINT_INFO("cam%d extrinsics = %.3f,%.3f,%.3f,%.3f | %.3f,%.3f,%.3f\n", (int)i, calib->quat()(0), calib->quat()(1), calib->quat()(2),
                 calib->quat()(3), calib->pos()(0), calib->pos()(1), calib->pos()(2));
    }
  }

  // Debug for imu intrinsics
  if (state->_options.do_calib_imu_intrinsics && state->_options.imu_model == StateOptions::ImuModel::KALIBR) {
    PRINT_INFO("q_GYROtoI = %.3f,%.3f,%.3f,%.3f\n", state->_calib_imu_GYROtoIMU->value()(0), state->_calib_imu_GYROtoIMU->value()(1),
               state->_calib_imu_GYROtoIMU->value()(2), state->_calib_imu_GYROtoIMU->value()(3));
  }
  if (state->_options.do_calib_imu_intrinsics && state->_options.imu_model == StateOptions::ImuModel::RPNG) {
    PRINT_INFO("q_ACCtoI = %.3f,%.3f,%.3f,%.3f\n", state->_calib_imu_ACCtoIMU->value()(0), state->_calib_imu_ACCtoIMU->value()(1),
               state->_calib_imu_ACCtoIMU->value()(2), state->_calib_imu_ACCtoIMU->value()(3));
  }
  if (state->_options.do_calib_imu_intrinsics && state->_options.imu_model == StateOptions::ImuModel::KALIBR) {
    PRINT_INFO("Dw = | %.4f,%.4f,%.4f | %.4f,%.4f | %.4f |\n", state->_calib_imu_dw->value()(0), state->_calib_imu_dw->value()(1),
               state->_calib_imu_dw->value()(2), state->_calib_imu_dw->value()(3), state->_calib_imu_dw->value()(4),
               state->_calib_imu_dw->value()(5));
    PRINT_INFO("Da = | %.4f,%.4f,%.4f | %.4f,%.4f | %.4f |\n", state->_calib_imu_da->value()(0), state->_calib_imu_da->value()(1),
               state->_calib_imu_da->value()(2), state->_calib_imu_da->value()(3), state->_calib_imu_da->value()(4),
               state->_calib_imu_da->value()(5));
  }
  if (state->_options.do_calib_imu_intrinsics && state->_options.imu_model == StateOptions::ImuModel::RPNG) {
    PRINT_INFO("Dw = | %.4f | %.4f,%.4f | %.4f,%.4f,%.4f |\n", state->_calib_imu_dw->value()(0), state->_calib_imu_dw->value()(1),
               state->_calib_imu_dw->value()(2), state->_calib_imu_dw->value()(3), state->_calib_imu_dw->value()(4),
               state->_calib_imu_dw->value()(5));
    PRINT_INFO("Da = | %.4f | %.4f,%.4f | %.4f,%.4f,%.4f |\n", state->_calib_imu_da->value()(0), state->_calib_imu_da->value()(1),
               state->_calib_imu_da->value()(2), state->_calib_imu_da->value()(3), state->_calib_imu_da->value()(4),
               state->_calib_imu_da->value()(5));
  }
  if (state->_options.do_calib_imu_intrinsics && state->_options.do_calib_imu_g_sensitivity) {
    PRINT_INFO("Tg = | %.4f,%.4f,%.4f |  %.4f,%.4f,%.4f | %.4f,%.4f,%.4f |\n", state->_calib_imu_tg->value()(0),
               state->_calib_imu_tg->value()(1), state->_calib_imu_tg->value()(2), state->_calib_imu_tg->value()(3),
               state->_calib_imu_tg->value()(4), state->_calib_imu_tg->value()(5), state->_calib_imu_tg->value()(6),
               state->_calib_imu_tg->value()(7), state->_calib_imu_tg->value()(8));
  }
}
