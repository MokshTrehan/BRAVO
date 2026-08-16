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
#include "TurnSafeCallbackInstaller.h"

#include "feat/Feature.h"
#include "feat/FeatureDatabase.h"
#include "feat/FeatureInitializer.h"
#include "track/TrackAruco.h"
#include "track/TrackDescriptor.h"
#include "track/TrackKLT.h"
#include "types/LandmarkRepresentation.h"
#include "utils/print.h"
#include "utils/quat_ops.h"

#include "init/InertialInitializer.h"

#include "state/Propagator.h"
#include "state/State.h"
#include "state/StateHelper.h"
#include "update/UpdaterZeroVelocity.h"

#include <cmath>
#include <stdexcept>

using namespace ov_core;
using namespace ov_type;
using namespace ov_msckf;

std::shared_ptr<State> VioManager::make_fresh_state_with_calibration() const {
  StateOptions options = params.state_options;
  std::shared_ptr<State> fresh = std::make_shared<State>(options);

  if (state) {
    fresh->_calib_imu_dw->set_value(state->_calib_imu_dw->value());
    fresh->_calib_imu_dw->set_fej(state->_calib_imu_dw->value());
    fresh->_calib_imu_da->set_value(state->_calib_imu_da->value());
    fresh->_calib_imu_da->set_fej(state->_calib_imu_da->value());
    fresh->_calib_imu_tg->set_value(state->_calib_imu_tg->value());
    fresh->_calib_imu_tg->set_fej(state->_calib_imu_tg->value());
    fresh->_calib_imu_GYROtoIMU->set_value(
        state->_calib_imu_GYROtoIMU->value());
    fresh->_calib_imu_GYROtoIMU->set_fej(
        state->_calib_imu_GYROtoIMU->value());
    fresh->_calib_imu_ACCtoIMU->set_value(
        state->_calib_imu_ACCtoIMU->value());
    fresh->_calib_imu_ACCtoIMU->set_fej(
        state->_calib_imu_ACCtoIMU->value());
    fresh->_calib_dt_CAMtoIMU->set_value(
        state->_calib_dt_CAMtoIMU->value());
    fresh->_calib_dt_CAMtoIMU->set_fej(
        state->_calib_dt_CAMtoIMU->value());
    fresh->_cam_intrinsics_cameras = state->_cam_intrinsics_cameras;
    for (int camera = 0; camera < options.num_cameras; ++camera) {
      fresh->_cam_intrinsics.at(camera)->set_value(
          state->_cam_intrinsics.at(camera)->value());
      fresh->_cam_intrinsics.at(camera)->set_fej(
          state->_cam_intrinsics.at(camera)->value());
      fresh->_calib_IMUtoCAM.at(camera)->set_value(
          state->_calib_IMUtoCAM.at(camera)->value());
      fresh->_calib_IMUtoCAM.at(camera)->set_fej(
          state->_calib_IMUtoCAM.at(camera)->value());
    }
  } else {
    fresh->_calib_imu_dw->set_value(params.vec_dw);
    fresh->_calib_imu_dw->set_fej(params.vec_dw);
    fresh->_calib_imu_da->set_value(params.vec_da);
    fresh->_calib_imu_da->set_fej(params.vec_da);
    fresh->_calib_imu_tg->set_value(params.vec_tg);
    fresh->_calib_imu_tg->set_fej(params.vec_tg);
    fresh->_calib_imu_GYROtoIMU->set_value(params.q_GYROtoIMU);
    fresh->_calib_imu_GYROtoIMU->set_fej(params.q_GYROtoIMU);
    fresh->_calib_imu_ACCtoIMU->set_value(params.q_ACCtoIMU);
    fresh->_calib_imu_ACCtoIMU->set_fej(params.q_ACCtoIMU);
    Eigen::VectorXd camera_to_imu_time(1);
    camera_to_imu_time(0) = params.calib_camimu_dt;
    fresh->_calib_dt_CAMtoIMU->set_value(camera_to_imu_time);
    fresh->_calib_dt_CAMtoIMU->set_fej(camera_to_imu_time);
    fresh->_cam_intrinsics_cameras = params.camera_intrinsics;
    for (int camera = 0; camera < options.num_cameras; ++camera) {
      fresh->_cam_intrinsics.at(camera)->set_value(
          params.camera_intrinsics.at(camera)->get_value());
      fresh->_cam_intrinsics.at(camera)->set_fej(
          params.camera_intrinsics.at(camera)->get_value());
      fresh->_calib_IMUtoCAM.at(camera)->set_value(
          params.camera_extrinsics.at(camera));
      fresh->_calib_IMUtoCAM.at(camera)->set_fej(
          params.camera_extrinsics.at(camera));
    }
  }
  return fresh;
}

void VioManager::reset_feature_epoch(const ov_core::CameraData &message) {
  const int per_camera_features = std::floor(
      static_cast<double>(params.num_pts) /
      static_cast<double>(params.state_options.num_cameras));
  if (params.use_klt) {
    trackFEATS = std::make_shared<TrackKLT>(
        state->_cam_intrinsics_cameras, per_camera_features,
        state->_options.max_aruco_features, params.use_stereo,
        params.histogram_method, params.fast_threshold, params.grid_x,
        params.grid_y, params.min_px_dist);
  } else {
    trackFEATS = std::make_shared<TrackDescriptor>(
        state->_cam_intrinsics_cameras, per_camera_features,
        state->_options.max_aruco_features, params.use_stereo,
        params.histogram_method, params.fast_threshold, params.grid_x,
        params.grid_y, params.min_px_dist, params.knn_ratio);
  }
  trackARUCO.reset();
  if (params.use_aruco) {
    trackARUCO = std::make_shared<TrackAruco>(
        state->_cam_intrinsics_cameras,
        state->_options.max_aruco_features, params.use_stereo,
        params.histogram_method, params.downsize_aruco);
  }
  initializer = std::make_shared<ov_init::InertialInitializer>(
      params.init_options, trackFEATS->get_feature_database());
  updaterZUPT.reset();
  if (params.try_zupt) {
    updaterZUPT = std::make_shared<UpdaterZeroVelocity>(
        params.zupt_options, params.imu_noises,
        trackFEATS->get_feature_database(), propagator, params.gravity_mag,
        params.zupt_max_velocity, params.zupt_noise_multiplier,
        params.zupt_max_disparity);
  }
  good_features_MSCKF.clear();
  active_tracks_time = -1.0;
  active_tracks_posinG.clear();
  active_tracks_uvd.clear();
  active_image.release();
  active_feat_linsys_A.clear();
  active_feat_linsys_b.clear();
  active_feat_linsys_count.clear();
  trackFEATS->feed_new_camera(message);
  if (trackARUCO) trackARUCO->feed_new_camera(message);
  if (turnsafe_t0_diagnostics && turnsafe_t0_diagnostics->active()) {
    // Seed the new tracker without emitting a duplicate frontend record for
    // the recovery callback. The old tracker remains alive in that callback's
    // envelope; the new observer begins on the next camera callback. Do not
    // touch the updater observer, whose installation is frozen after use.
    const std::shared_ptr<TrackKLT> klt =
        std::dynamic_pointer_cast<TrackKLT>(trackFEATS);
    if (!reinstall_turnsafe_t0_frontend_callback(turnsafe_t0_diagnostics,
                                                  klt.get())) {
      PRINT_WARNING(
          YELLOW "[TURNSAFE-T0]: status=epoch_frontend_rebind_rejected "
                 "estimator_unchanged=1\n" RESET);
    }
  }
}

void VioManager::commit_long_gap_recovery(
    const ov_core::CameraData &message, const Eigen::Matrix3d &R_GtoI,
    const Eigen::Vector3d &p_IinG) {
  if (!R_GtoI.allFinite() || !p_IinG.allFinite() || !state ||
      !std::isfinite(message.timestamp)) {
    throw std::runtime_error("invalid long-gap recovery commit input");
  }

  const Eigen::MatrixXd old_imu_covariance =
      StateHelper::get_marginal_covariance(state, {state->_imu});
  if (old_imu_covariance.rows() != 15 ||
      old_imu_covariance.cols() != 15 ||
      !old_imu_covariance.allFinite()) {
    throw std::runtime_error(
        "long-gap recovery could not preserve bias covariance");
  }
  const Eigen::Matrix<double, 16, 1> old_imu = state->_imu->value();
  std::shared_ptr<State> recovered = make_fresh_state_with_calibration();

  Eigen::Matrix<double, 16, 1> recovered_imu = old_imu;
  recovered_imu.block<4, 1>(0, 0) = ov_core::rot_2_quat(R_GtoI);
  recovered_imu.block<3, 1>(4, 0) = p_IinG;
  recovered_imu.block<3, 1>(7, 0).setZero();
  recovered->_imu->set_value(recovered_imu);
  recovered->_imu->set_fej(recovered_imu);

  Eigen::Matrix<double, 15, 15> covariance =
      Eigen::Matrix<double, 15, 15>::Zero();
  const double radians_per_degree = std::acos(-1.0) / 180.0;
  const double orientation_sigma =
      params.long_gap_recovery_orientation_sigma_deg * radians_per_degree;
  covariance.block<3, 3>(0, 0) =
      orientation_sigma * orientation_sigma * Eigen::Matrix3d::Identity();
  covariance.block<3, 3>(3, 3) =
      params.long_gap_recovery_position_sigma_m *
      params.long_gap_recovery_position_sigma_m *
      Eigen::Matrix3d::Identity();
  covariance.block<3, 3>(6, 6) =
      params.long_gap_recovery_velocity_sigma_mps *
      params.long_gap_recovery_velocity_sigma_mps *
      Eigen::Matrix3d::Identity();
  covariance.block<6, 6>(9, 9) = old_imu_covariance.block<6, 6>(9, 9);
  StateHelper::set_initial_covariance(recovered, covariance,
                                      {recovered->_imu});
  const Eigen::Matrix3d committed_position_covariance =
      covariance.block<3, 3>(3, 3);
  recovered->_timestamp = message.timestamp;

  state = std::move(recovered);
  propagator->invalidate_cache();
  propagator->clean_old_imu_measurements(
      message.timestamp + state->_calib_dt_CAMtoIMU->value()(0) - 0.10);
  startup_time = message.timestamp;
  // Make the accepted relocalization state immediately observable.  Keeping
  // this at -1 suppresses ROS/state-file output until visual warm-up and
  // prevents the prospectively frozen first-resumed anchor and covariance
  // checks from evaluating the actual recovery commit.
  timelastupdate = message.timestamp;
  is_initialized_vio = true;
  did_zupt_update = false;
  has_moved_since_zupt = false;
  {
    std::lock_guard<std::mutex> lock(camera_queue_init_mtx);
    camera_queue_init.clear();
  }
  reset_feature_epoch(message);

  ++epoch_id;
  ++long_gap_recovery_commit_count;
  long_gap_recovery_phase = LongGapRecoveryPhase::WARMUP;
  long_gap_recovery_consensus.clear();

  PRINT_INFO(
      "[LONG-GAP-RECOVERY]: event=relocalization_commit epoch=%llu "
      "timestamp=%.15f p_x=%.9f p_y=%.9f p_z=%.9f velocity_reset=1\n",
      static_cast<unsigned long long>(epoch_id), message.timestamp,
      p_IinG(0), p_IinG(1), p_IinG(2));
  PRINT_INFO(
      "[LONG-GAP-RECOVERY]: event=first_resumed_covariance epoch=%llu "
      "camera_timestamp=%.15f state_output_timestamp=%.15f "
      "frame=estimator_global available=1 "
      "p_cov_00=%.17g p_cov_01=%.17g p_cov_02=%.17g "
      "p_cov_10=%.17g p_cov_11=%.17g p_cov_12=%.17g "
      "p_cov_20=%.17g p_cov_21=%.17g p_cov_22=%.17g\n",
      static_cast<unsigned long long>(epoch_id), message.timestamp,
      message.timestamp + state->_calib_dt_CAMtoIMU->value()(0),
      committed_position_covariance(0, 0),
      committed_position_covariance(0, 1),
      committed_position_covariance(0, 2),
      committed_position_covariance(1, 0),
      committed_position_covariance(1, 1),
      committed_position_covariance(1, 2),
      committed_position_covariance(2, 0),
      committed_position_covariance(2, 1),
      committed_position_covariance(2, 2));
}

void VioManager::initialize_with_gt(Eigen::Matrix<double, 17, 1> imustate) {

  // Initialize the system
  state->_imu->set_value(imustate.block(1, 0, 16, 1));
  state->_imu->set_fej(imustate.block(1, 0, 16, 1));

  // Fix the global yaw and position gauge freedoms
  // TODO: Why does this break out simulation consistency metrics?
  std::vector<std::shared_ptr<ov_type::Type>> order = {state->_imu};
  Eigen::MatrixXd Cov = std::pow(0.02, 2) * Eigen::MatrixXd::Identity(state->_imu->size(), state->_imu->size());
  Cov.block(0, 0, 3, 3) = std::pow(0.017, 2) * Eigen::Matrix3d::Identity(); // q
  Cov.block(3, 3, 3, 3) = std::pow(0.05, 2) * Eigen::Matrix3d::Identity();  // p
  Cov.block(6, 6, 3, 3) = std::pow(0.01, 2) * Eigen::Matrix3d::Identity();  // v (static)
  StateHelper::set_initial_covariance(state, Cov, order);

  // Set the state time
  state->_timestamp = imustate(0, 0);
  startup_time = imustate(0, 0);
  is_initialized_vio = true;

  // Cleanup any features older then the initialization time
  trackFEATS->get_feature_database()->cleanup_measurements(state->_timestamp);
  if (trackARUCO != nullptr) {
    trackARUCO->get_feature_database()->cleanup_measurements(state->_timestamp);
  }

  // Print what we init'ed with
  PRINT_DEBUG(GREEN "[INIT]: INITIALIZED FROM GROUNDTRUTH FILE!!!!!\n" RESET);
  PRINT_DEBUG(GREEN "[INIT]: orientation = %.4f, %.4f, %.4f, %.4f\n" RESET, state->_imu->quat()(0), state->_imu->quat()(1),
              state->_imu->quat()(2), state->_imu->quat()(3));
  PRINT_DEBUG(GREEN "[INIT]: bias gyro = %.4f, %.4f, %.4f\n" RESET, state->_imu->bias_g()(0), state->_imu->bias_g()(1),
              state->_imu->bias_g()(2));
  PRINT_DEBUG(GREEN "[INIT]: velocity = %.4f, %.4f, %.4f\n" RESET, state->_imu->vel()(0), state->_imu->vel()(1), state->_imu->vel()(2));
  PRINT_DEBUG(GREEN "[INIT]: bias accel = %.4f, %.4f, %.4f\n" RESET, state->_imu->bias_a()(0), state->_imu->bias_a()(1),
              state->_imu->bias_a()(2));
  PRINT_DEBUG(GREEN "[INIT]: position = %.4f, %.4f, %.4f\n" RESET, state->_imu->pos()(0), state->_imu->pos()(1), state->_imu->pos()(2));
}

bool VioManager::try_to_initialize(const ov_core::CameraData &message) {

  // Directly return if the initialization thread is running
  // Note that we lock on the queue since we could have finished an update
  // And are using this queue to propagate the state forward. We should wait in this case
  if (thread_init_running) {
    std::lock_guard<std::mutex> lck(camera_queue_init_mtx);
    camera_queue_init.push_back(message.timestamp);
    return false;
  }

  // If the thread was a success, then return success!
  if (thread_init_success) {
    return true;
  }

  // Run the initialization in a second thread so it can go as slow as it desires
  thread_init_running = true;
  std::thread thread([&] {
    // Returns from our initializer
    double timestamp;
    Eigen::MatrixXd covariance;
    std::vector<std::shared_ptr<ov_type::Type>> order;
    auto init_rT1 = boost::posix_time::microsec_clock::local_time();

    // Try to initialize the system
    // We will wait for a jerk if we do not have the zero velocity update enabled
    // Otherwise we can initialize right away as the zero velocity will handle the stationary case
    bool wait_for_jerk = (updaterZUPT == nullptr);
    bool success = initializer->initialize(timestamp, covariance, order, state->_imu, wait_for_jerk);

    // If we have initialized successfully we will set the covariance and state elements as needed
    // TODO: set the clones and SLAM features here so we can start updating right away...
    if (success) {

      // Set our covariance (state should already be set in the initializer)
      StateHelper::set_initial_covariance(state, covariance, order);

      // Set the state time
      state->_timestamp = timestamp;
      startup_time = timestamp;

      // Cleanup any features older than the initialization time
      // Also increase the number of features to the desired amount during estimation
      // NOTE: we will split the total number of features over all cameras uniformly
      trackFEATS->get_feature_database()->cleanup_measurements(state->_timestamp);
      trackFEATS->set_num_features(std::floor((double)params.num_pts / (double)params.state_options.num_cameras));
      if (trackARUCO != nullptr) {
        trackARUCO->get_feature_database()->cleanup_measurements(state->_timestamp);
      }

      // If we are moving then don't do zero velocity update4
      if (state->_imu->vel().norm() > params.zupt_max_velocity) {
        has_moved_since_zupt = true;
      }

      // Else we are good to go, print out our stats
      auto init_rT2 = boost::posix_time::microsec_clock::local_time();
      PRINT_INFO(GREEN "[init]: successful initialization in %.4f seconds\n" RESET, (init_rT2 - init_rT1).total_microseconds() * 1e-6);
      PRINT_INFO(GREEN "[init]: orientation = %.4f, %.4f, %.4f, %.4f\n" RESET, state->_imu->quat()(0), state->_imu->quat()(1),
                 state->_imu->quat()(2), state->_imu->quat()(3));
      PRINT_INFO(GREEN "[init]: bias gyro = %.4f, %.4f, %.4f\n" RESET, state->_imu->bias_g()(0), state->_imu->bias_g()(1),
                 state->_imu->bias_g()(2));
      PRINT_INFO(GREEN "[init]: velocity = %.4f, %.4f, %.4f\n" RESET, state->_imu->vel()(0), state->_imu->vel()(1), state->_imu->vel()(2));
      PRINT_INFO(GREEN "[init]: bias accel = %.4f, %.4f, %.4f\n" RESET, state->_imu->bias_a()(0), state->_imu->bias_a()(1),
                 state->_imu->bias_a()(2));
      PRINT_INFO(GREEN "[init]: position = %.4f, %.4f, %.4f\n" RESET, state->_imu->pos()(0), state->_imu->pos()(1), state->_imu->pos()(2));

      // Remove any camera times that are order then the initialized time
      // This can happen if the initialization has taken a while to perform
      std::lock_guard<std::mutex> lck(camera_queue_init_mtx);
      std::vector<double> camera_timestamps_to_init;
      for (size_t i = 0; i < camera_queue_init.size(); i++) {
        if (camera_queue_init.at(i) > timestamp) {
          camera_timestamps_to_init.push_back(camera_queue_init.at(i));
        }
      }

      // Now we have initialized we will propagate the state to the current timestep
      // In general this should be ok as long as the initialization didn't take too long to perform
      // Propagating over multiple seconds will become an issue if the initial biases are bad
      size_t clone_rate = (size_t)((double)camera_timestamps_to_init.size() / (double)params.state_options.max_clone_size) + 1;
      for (size_t i = 0; i < camera_timestamps_to_init.size(); i += clone_rate) {
        propagator->propagate_and_clone(state, camera_timestamps_to_init.at(i));
        StateHelper::marginalize_old_clone(state);
      }
      PRINT_DEBUG(YELLOW "[init]: moved the state forward %.2f seconds\n" RESET, state->_timestamp - timestamp);
      thread_init_success = true;
      camera_queue_init.clear();

    } else {
      auto init_rT2 = boost::posix_time::microsec_clock::local_time();
      PRINT_DEBUG(YELLOW "[init]: failed initialization in %.4f seconds\n" RESET, (init_rT2 - init_rT1).total_microseconds() * 1e-6);
      thread_init_success = false;
      std::lock_guard<std::mutex> lck(camera_queue_init_mtx);
      camera_queue_init.clear();
    }

    // Finally, mark that the thread has finished running
    thread_init_running = false;
  });

  // If we are single threaded, then run single threaded
  // Otherwise detach this thread so it runs in the background!
  if (!params.use_multi_threading_subs) {
    thread.join();
  } else {
    thread.detach();
  }
  return false;
}

void VioManager::retriangulate_active_tracks(const ov_core::CameraData &message) {

  // Start timing
  boost::posix_time::ptime retri_rT1, retri_rT2, retri_rT3;
  retri_rT1 = boost::posix_time::microsec_clock::local_time();

  // Clear old active track data
  assert(state->_clones_IMU.find(message.timestamp) != state->_clones_IMU.end());
  active_tracks_time = message.timestamp;
  active_image = cv::Mat();
  trackFEATS->display_active(active_image, 255, 255, 255, 255, 255, 255, " ");
  if (!active_image.empty()) {
    active_image = active_image(cv::Rect(0, 0, message.images.at(0).cols, message.images.at(0).rows));
  }
  active_tracks_posinG.clear();
  active_tracks_uvd.clear();

  // Current active tracks in our frontend
  // TODO: should probably assert here that these are at the message time...
  auto last_obs = trackFEATS->get_last_obs();
  auto last_ids = trackFEATS->get_last_ids();

  // New set of linear systems that only contain the latest track info
  std::map<size_t, Eigen::Matrix3d> active_feat_linsys_A_new;
  std::map<size_t, Eigen::Vector3d> active_feat_linsys_b_new;
  std::map<size_t, int> active_feat_linsys_count_new;
  std::unordered_map<size_t, Eigen::Vector3d> active_tracks_posinG_new;

  // Append our new observations for each camera
  std::map<size_t, cv::Point2f> feat_uvs_in_cam0;
  for (auto const &cam_id : message.sensor_ids) {

    // IMU historical clone
    Eigen::Matrix3d R_GtoI = state->_clones_IMU.at(active_tracks_time)->Rot();
    Eigen::Vector3d p_IinG = state->_clones_IMU.at(active_tracks_time)->pos();

    // Calibration for this cam_id
    Eigen::Matrix3d R_ItoC = state->_calib_IMUtoCAM.at(cam_id)->Rot();
    Eigen::Vector3d p_IinC = state->_calib_IMUtoCAM.at(cam_id)->pos();

    // Convert current CAMERA position relative to global
    Eigen::Matrix3d R_GtoCi = R_ItoC * R_GtoI;
    Eigen::Vector3d p_CiinG = p_IinG - R_GtoCi.transpose() * p_IinC;

    // Loop through each measurement
    assert(last_obs.find(cam_id) != last_obs.end());
    assert(last_ids.find(cam_id) != last_ids.end());
    for (size_t i = 0; i < last_obs.at(cam_id).size(); i++) {

      // Record this feature uv if is seen from cam0
      size_t featid = last_ids.at(cam_id).at(i);
      cv::Point2f pt_d = last_obs.at(cam_id).at(i).pt;
      if (cam_id == 0) {
        feat_uvs_in_cam0[featid] = pt_d;
      }

      // Skip this feature if it is a SLAM feature (the state estimate takes priority)
      if (state->_features_SLAM.find(featid) != state->_features_SLAM.end()) {
        continue;
      }

      // Get the UV coordinate normal
      cv::Point2f pt_n = state->_cam_intrinsics_cameras.at(cam_id)->undistort_cv(pt_d);
      Eigen::Matrix<double, 3, 1> b_i;
      b_i << pt_n.x, pt_n.y, 1;
      b_i = R_GtoCi.transpose() * b_i;
      b_i = b_i / b_i.norm();
      Eigen::Matrix3d Bperp = skew_x(b_i);

      // Append to our linear system
      Eigen::Matrix3d Ai = Bperp.transpose() * Bperp;
      Eigen::Vector3d bi = Ai * p_CiinG;
      if (active_feat_linsys_A.find(featid) == active_feat_linsys_A.end()) {
        active_feat_linsys_A_new.insert({featid, Ai});
        active_feat_linsys_b_new.insert({featid, bi});
        active_feat_linsys_count_new.insert({featid, 1});
      } else {
        active_feat_linsys_A_new[featid] = Ai + active_feat_linsys_A[featid];
        active_feat_linsys_b_new[featid] = bi + active_feat_linsys_b[featid];
        active_feat_linsys_count_new[featid] = 1 + active_feat_linsys_count[featid];
      }

      // For this feature, recover its 3d position if we have enough observations!
      if (active_feat_linsys_count_new.at(featid) > 3) {

        // Recover feature estimate
        Eigen::Matrix3d A = active_feat_linsys_A_new[featid];
        Eigen::Vector3d b = active_feat_linsys_b_new[featid];
        Eigen::MatrixXd p_FinG = A.colPivHouseholderQr().solve(b);
        Eigen::MatrixXd p_FinCi = R_GtoCi * (p_FinG - p_CiinG);

        // Check A and p_FinCi
        Eigen::JacobiSVD<Eigen::Matrix3d> svd(A);
        Eigen::MatrixXd singularValues;
        singularValues.resize(svd.singularValues().rows(), 1);
        singularValues = svd.singularValues();
        double condA = singularValues(0, 0) / singularValues(singularValues.rows() - 1, 0);

        // If we have a bad condition number, or it is too close
        // Then set the flag for bad (i.e. set z-axis to nan)
        if (std::abs(condA) <= params.featinit_options.max_cond_number && p_FinCi(2, 0) >= params.featinit_options.min_dist &&
            p_FinCi(2, 0) <= params.featinit_options.max_dist && !std::isnan(p_FinCi.norm())) {
          active_tracks_posinG_new[featid] = p_FinG;
        }
      }
    }
  }
  size_t total_triangulated = active_tracks_posinG.size();

  // Update active set of linear systems
  active_feat_linsys_A = active_feat_linsys_A_new;
  active_feat_linsys_b = active_feat_linsys_b_new;
  active_feat_linsys_count = active_feat_linsys_count_new;
  active_tracks_posinG = active_tracks_posinG_new;
  retri_rT2 = boost::posix_time::microsec_clock::local_time();

  // Return if no features
  if (active_tracks_posinG.empty() && state->_features_SLAM.empty())
    return;

  // Append our SLAM features we have
  for (const auto &feat : state->_features_SLAM) {
    Eigen::Vector3d p_FinG = feat.second->get_xyz(false);
    if (LandmarkRepresentation::is_relative_representation(feat.second->_feat_representation)) {
      // Assert that we have an anchor pose for this feature
      assert(feat.second->_anchor_cam_id != -1);
      // Get calibration for our anchor camera
      Eigen::Matrix3d R_ItoC = state->_calib_IMUtoCAM.at(feat.second->_anchor_cam_id)->Rot();
      Eigen::Vector3d p_IinC = state->_calib_IMUtoCAM.at(feat.second->_anchor_cam_id)->pos();
      // Anchor pose orientation and position
      Eigen::Matrix3d R_GtoI = state->_clones_IMU.at(feat.second->_anchor_clone_timestamp)->Rot();
      Eigen::Vector3d p_IinG = state->_clones_IMU.at(feat.second->_anchor_clone_timestamp)->pos();
      // Feature in the global frame
      p_FinG = R_GtoI.transpose() * R_ItoC.transpose() * (feat.second->get_xyz(false) - p_IinC) + p_IinG;
    }
    active_tracks_posinG[feat.second->_featid] = p_FinG;
  }

  // Calibration of the first camera (cam0)
  std::shared_ptr<Vec> distortion = state->_cam_intrinsics.at(0);
  std::shared_ptr<PoseJPL> calibration = state->_calib_IMUtoCAM.at(0);
  Eigen::Matrix<double, 3, 3> R_ItoC = calibration->Rot();
  Eigen::Matrix<double, 3, 1> p_IinC = calibration->pos();

  // Get current IMU clone state
  std::shared_ptr<PoseJPL> clone_Ii = state->_clones_IMU.at(active_tracks_time);
  Eigen::Matrix3d R_GtoIi = clone_Ii->Rot();
  Eigen::Vector3d p_IiinG = clone_Ii->pos();

  // 4. Next we can update our variable with the global position
  //    We also will project the features into the current frame
  for (const auto &feat : active_tracks_posinG) {

    // For now skip features not seen from current frame
    // TODO: should we publish other features not tracked in cam0??
    if (feat_uvs_in_cam0.find(feat.first) == feat_uvs_in_cam0.end())
      continue;

    // Calculate the depth of the feature in the current frame
    // Project SLAM feature and non-cam0 features into the current frame of reference
    Eigen::Vector3d p_FinIi = R_GtoIi * (feat.second - p_IiinG);
    Eigen::Vector3d p_FinCi = R_ItoC * p_FinIi + p_IinC;
    double depth = p_FinCi(2);
    Eigen::Vector2d uv_dist;
    if (feat_uvs_in_cam0.find(feat.first) != feat_uvs_in_cam0.end()) {
      uv_dist << (double)feat_uvs_in_cam0.at(feat.first).x, (double)feat_uvs_in_cam0.at(feat.first).y;
    } else {
      Eigen::Vector2d uv_norm;
      uv_norm << p_FinCi(0) / depth, p_FinCi(1) / depth;
      uv_dist = state->_cam_intrinsics_cameras.at(0)->distort_d(uv_norm);
    }

    // Skip if not valid (i.e. negative depth, or outside of image)
    if (depth < 0.1) {
      continue;
    }

    // Skip if not valid (i.e. negative depth, or outside of image)
    int width = state->_cam_intrinsics_cameras.at(0)->w();
    int height = state->_cam_intrinsics_cameras.at(0)->h();
    if (uv_dist(0) < 0 || (int)uv_dist(0) >= width || uv_dist(1) < 0 || (int)uv_dist(1) >= height) {
      // PRINT_DEBUG("feat %zu -> depth = %.2f | u_d = %.2f | v_d = %.2f\n",(*it2)->featid,depth,uv_dist(0),uv_dist(1));
      continue;
    }

    // Finally construct the uv and depth
    Eigen::Vector3d uvd;
    uvd << uv_dist, depth;
    active_tracks_uvd.insert({feat.first, uvd});
  }
  retri_rT3 = boost::posix_time::microsec_clock::local_time();

  // Timing information
  PRINT_ALL(CYAN "[RETRI-TIME]: %.4f seconds for triangulation (%zu tri of %zu active)\n" RESET,
            (retri_rT2 - retri_rT1).total_microseconds() * 1e-6, total_triangulated, active_feat_linsys_A.size());
  PRINT_ALL(CYAN "[RETRI-TIME]: %.4f seconds for re-projection into current\n" RESET, (retri_rT3 - retri_rT2).total_microseconds() * 1e-6);
  PRINT_ALL(CYAN "[RETRI-TIME]: %.4f seconds total\n" RESET, (retri_rT3 - retri_rT1).total_microseconds() * 1e-6);
}

cv::Mat VioManager::get_historical_viz_image() {

  // Return if not ready yet
  if (state == nullptr || trackFEATS == nullptr)
    return cv::Mat();

  // Build an id-list of what features we should highlight (i.e. SLAM)
  std::vector<size_t> highlighted_ids;
  for (const auto &feat : state->_features_SLAM) {
    highlighted_ids.push_back(feat.first);
  }

  // Text we will overlay if needed
  std::string overlay = (did_zupt_update) ? "zvupt" : "";
  overlay = (!is_initialized_vio) ? "init" : overlay;

  // Get the current active tracks
  cv::Mat img_history;
  trackFEATS->display_history(img_history, 255, 255, 0, 255, 255, 255, highlighted_ids, overlay);
  if (trackARUCO != nullptr) {
    trackARUCO->display_history(img_history, 0, 255, 255, 255, 255, 255, highlighted_ids, overlay);
    // trackARUCO->display_active(img_history, 0, 255, 255, 255, 255, 255, overlay);
  }

  // Finally return the image
  return img_history;
}

std::vector<Eigen::Vector3d> VioManager::get_features_SLAM() {
  std::vector<Eigen::Vector3d> slam_feats;
  for (auto &f : state->_features_SLAM) {
    if ((int)f.first <= 4 * state->_options.max_aruco_features)
      continue;
    if (ov_type::LandmarkRepresentation::is_relative_representation(f.second->_feat_representation)) {
      // Assert that we have an anchor pose for this feature
      assert(f.second->_anchor_cam_id != -1);
      // Get calibration for our anchor camera
      Eigen::Matrix<double, 3, 3> R_ItoC = state->_calib_IMUtoCAM.at(f.second->_anchor_cam_id)->Rot();
      Eigen::Matrix<double, 3, 1> p_IinC = state->_calib_IMUtoCAM.at(f.second->_anchor_cam_id)->pos();
      // Anchor pose orientation and position
      Eigen::Matrix<double, 3, 3> R_GtoI = state->_clones_IMU.at(f.second->_anchor_clone_timestamp)->Rot();
      Eigen::Matrix<double, 3, 1> p_IinG = state->_clones_IMU.at(f.second->_anchor_clone_timestamp)->pos();
      // Feature in the global frame
      slam_feats.push_back(R_GtoI.transpose() * R_ItoC.transpose() * (f.second->get_xyz(false) - p_IinC) + p_IinG);
    } else {
      slam_feats.push_back(f.second->get_xyz(false));
    }
  }
  return slam_feats;
}

std::vector<Eigen::Vector3d> VioManager::get_features_ARUCO() {
  std::vector<Eigen::Vector3d> aruco_feats;
  for (auto &f : state->_features_SLAM) {
    if ((int)f.first > 4 * state->_options.max_aruco_features)
      continue;
    if (ov_type::LandmarkRepresentation::is_relative_representation(f.second->_feat_representation)) {
      // Assert that we have an anchor pose for this feature
      assert(f.second->_anchor_cam_id != -1);
      // Get calibration for our anchor camera
      Eigen::Matrix<double, 3, 3> R_ItoC = state->_calib_IMUtoCAM.at(f.second->_anchor_cam_id)->Rot();
      Eigen::Matrix<double, 3, 1> p_IinC = state->_calib_IMUtoCAM.at(f.second->_anchor_cam_id)->pos();
      // Anchor pose orientation and position
      Eigen::Matrix<double, 3, 3> R_GtoI = state->_clones_IMU.at(f.second->_anchor_clone_timestamp)->Rot();
      Eigen::Matrix<double, 3, 1> p_IinG = state->_clones_IMU.at(f.second->_anchor_clone_timestamp)->pos();
      // Feature in the global frame
      aruco_feats.push_back(R_GtoI.transpose() * R_ItoC.transpose() * (f.second->get_xyz(false) - p_IinC) + p_IinG);
    } else {
      aruco_feats.push_back(f.second->get_xyz(false));
    }
  }
  return aruco_feats;
}
