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

#include <ros/ros.h>
#include <rosbag/bag.h>
#include <rosbag/view.h>
#include <sensor_msgs/Image.h>
#include <sensor_msgs/Imu.h>

#include <algorithm>
#include <array>
#include <cerrno>
#include <cmath>
#include <cstdio>
#include <cstdint>
#include <cstdlib>
#include <exception>
#include <fcntl.h>
#include <iomanip>
#include <limits>
#include <locale>
#include <map>
#include <memory>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <sys/stat.h>
#include <unistd.h>
#include <utility>
#include <vector>

#include "core/VioManager.h"
#include "core/VioManagerOptions.h"
#include "ros/CP2ROS1RuntimeParameters.h"
#include "ros/ROS1Visualizer.h"
#include "update/CP2RuntimeContext.h"
#include "update/CP2OfflineReplay.h"
#include "update/CP2OutputCapability.h"
#include "update/CP2SerialPairing.h"
#include "update/CP2SerialRuntimeTrace.h"
#include "update/CP2TraceJournal.h"
#include "utils/dataset_reader.h"
#include "utils/KaistVioSerialPairing.h"

using namespace ov_msckf;

namespace {

bool cp2_time_nanoseconds(const ros::Time &time,
                          std::uint64_t &output) noexcept {
  return CP2SerialPairSelector::ComposeNanoseconds(
      static_cast<std::uint64_t>(time.sec),
      static_cast<std::uint64_t>(time.nsec), output);
}

CP2SerialRuntimeEnqueueStatus cp2_runtime_enqueue_status(
    CP2SerialEnqueueStatus status) {
  switch (status) {
  case CP2SerialEnqueueStatus::kQueued:
    return CP2SerialRuntimeEnqueueStatus::kQueued;
  case CP2SerialEnqueueStatus::kFrequencyDropped:
    return CP2SerialRuntimeEnqueueStatus::kFrequencyDropped;
  case CP2SerialEnqueueStatus::kCam0DecodeFailed:
    return CP2SerialRuntimeEnqueueStatus::kCam0DecodeFailed;
  case CP2SerialEnqueueStatus::kCam1DecodeFailed:
    return CP2SerialRuntimeEnqueueStatus::kCam1DecodeFailed;
  }
  throw std::logic_error("invalid CP2 serial enqueue status");
}

bool read_cp2_output_capabilities(
    ros::NodeHandle &node,
    CP2RuntimeOutputCapabilities &output) noexcept {
  struct Binding {
    const char *parameter;
    CP2OutputCapability CP2RuntimeOutputCapabilities::*member;
  };
  static const std::array<Binding, 14U> bindings = {{
      {"cp2_serial_trace_sink_capability",
       &CP2RuntimeOutputCapabilities::serial_trace},
      {"cp2_callback_trace_sink_capability",
       &CP2RuntimeOutputCapabilities::callback_trace},
      {"cp2_trajectory_trace_sink_capability",
       &CP2RuntimeOutputCapabilities::trajectory_trace},
      {"cp2_updater_trace_sink_capability",
       &CP2RuntimeOutputCapabilities::updater_trace},
      {"cp2_state_payload_sink_capability",
       &CP2RuntimeOutputCapabilities::state_payload},
      {"cp2_proposal_payload_sink_capability",
       &CP2RuntimeOutputCapabilities::proposal_payload},
      {"cp2_raw_system_payload_sink_capability",
       &CP2RuntimeOutputCapabilities::raw_system_payload},
      {"cp2_timing_trace_sink_capability",
       &CP2RuntimeOutputCapabilities::timing_trace},
      {"cp2_runtime_parameters_sink_capability",
       &CP2RuntimeOutputCapabilities::runtime_parameters},
      {"cp2_loader_map_before_sink_capability",
       &CP2RuntimeOutputCapabilities::loader_map_before},
      {"cp2_loader_map_after_sink_capability",
       &CP2RuntimeOutputCapabilities::loader_map_after},
      {"cp2_legacy_state_sink_capability",
       &CP2RuntimeOutputCapabilities::legacy_state},
      {"cp2_legacy_deviation_sink_capability",
       &CP2RuntimeOutputCapabilities::legacy_deviation},
      {"cp2_legacy_timing_sink_capability",
       &CP2RuntimeOutputCapabilities::legacy_timing},
  }};
  try {
    CP2RuntimeOutputCapabilities parsed;
    for (const Binding &binding : bindings) {
      std::string encoded;
      if (!node.getParam(binding.parameter, encoded) ||
          !ParseCP2OutputCapability(encoded, parsed.*(binding.member))) {
        output = CP2RuntimeOutputCapabilities{};
        return false;
      }
    }
    output = std::move(parsed);
    return true;
  } catch (...) {
    output = CP2RuntimeOutputCapabilities{};
    return false;
  }
}

} // namespace

// Main function
int main(int argc, char **argv) {

  // Offline replay is an exact, mutually exclusive pre-ROS mode.
  if (argc > 1 && std::string(argv[1]) == "--cp2-offline-replay") {
    CP2OfflineReplayPaths replay_paths;
    const CP2OfflineReplayResult parsed =
        ParseCP2OfflineReplayArguments(argc, argv, replay_paths);
    if (!parsed.accepted()) {
      std::fprintf(stderr, "CP2 offline replay rejected: %s: %s\n",
                   cp2_offline_replay_status_name(parsed.status),
                   parsed.detail.c_str());
      return EXIT_FAILURE;
    }
    const CP2OfflineReplayResult replay = RunCP2OfflineReplay(replay_paths);
    if (!replay.accepted()) {
      std::fprintf(stderr, "CP2 offline replay failed: %s: %s\n",
                   cp2_offline_replay_status_name(replay.status),
                   replay.detail.c_str());
      return EXIT_FAILURE;
    }
    return EXIT_SUCCESS;
  }

  // Ensure we have a path, if the user passes it then we should use it
  std::string config_path = "unset_path_to_config.yaml";
  if (argc > 1) {
    config_path = argv[1];
  }

  // Launch our ros node
  ros::init(argc, argv, "ros1_serial_msckf");
  auto nh = std::make_shared<ros::NodeHandle>("~");
  nh->param<std::string>("config_path", config_path, config_path);

  // Load the config
  auto parser = std::make_shared<ov_core::YamlParser>(config_path);
  parser->set_node_handler(nh);

  // Verbosity
  std::string verbosity = "INFO";
  parser->parse_config("verbosity", verbosity);
  ov_core::Printer::setPrintLevel(verbosity);

  // Create our VIO system
  VioManagerOptions params;
  params.print_and_load(parser);
  // The serial runner cannot safely detach the visualizer's publishing thread:
  // it has no join/ownership protocol and captures the visualizer by reference.
  params.use_multi_threading_pubs = false;
  params.use_multi_threading_subs = false;

  //===================================================================================
  //===================================================================================
  //===================================================================================

  // Our imu topic
  std::string topic_imu;
  nh->param<std::string>("topic_imu", topic_imu, "/imu0");
  parser->parse_external("relative_config_imu", "imu0", "rostopic", topic_imu);
  PRINT_DEBUG("[SERIAL]: imu: %s\n", topic_imu.c_str());

  // Our camera topics
  std::vector<std::string> topic_cameras;
  for (int i = 0; i < params.state_options.num_cameras; i++) {
    std::string cam_topic;
    nh->param<std::string>("topic_camera" + std::to_string(i), cam_topic, "/cam" + std::to_string(i) + "/image_raw");
    parser->parse_external("relative_config_imucam", "cam" + std::to_string(i), "rostopic", cam_topic);
    topic_cameras.emplace_back(cam_topic);
    PRINT_DEBUG("[SERIAL]: cam: %s\n", cam_topic.c_str());
  }

  // Get our start location and how much of the bag we want to play
  // Make the bag duration < 0 to just process to the end of the bag
  double bag_start, bag_durr;
  nh->param<double>("bag_start", bag_start, 0);
  nh->param<double>("bag_durr", bag_durr, -1);
  PRINT_DEBUG("[SERIAL]: bag start: %.1f\n", bag_start);
  PRINT_DEBUG("[SERIAL]: bag duration: %.1f\n", bag_durr);

  // KAIST-VIO camera measurement times are exact equal header stamps, while
  // their rosbag record times can differ by more than the legacy 20 ms
  // first-forward window. This dataset-specific path is default-off and only
  // selects existing messages; it never rewrites pixels or timestamps.
  bool kaist_vio_exact_header_stereo = false;
  nh->param<bool>("kaist_vio_exact_header_stereo",
                  kaist_vio_exact_header_stereo, false);
  if (kaist_vio_exact_header_stereo &&
      (params.state_options.num_cameras != 2 || topic_cameras.size() != 2U ||
       topic_cameras[0] == topic_cameras[1])) {
    PRINT_ERROR(RED "[SERIAL-KAIST]: exact-header mode requires two distinct camera topics\n" RESET);
    ros::shutdown();
    return EXIT_FAILURE;
  }
  if (kaist_vio_exact_header_stereo && params.use_multi_threading_subs) {
    PRINT_ERROR(RED "[SERIAL-KAIST]: exact-header mode requires synchronous subscriber processing\n" RESET);
    ros::shutdown();
    return EXIT_FAILURE;
  }

  // CP2 evidence mode is identified only by the frozen launch parameter. Its
  // strict context is read and bound before constructing any object that can
  // create an output sink and before constructing/opening a rosbag object.
  bool cp2_evidence_mode = false;
  CP2RuntimeContext cp2_context;
  CP2RuntimeOutputCapabilities cp2_output_capabilities;
  std::string cp2_trace_level;
  if (nh->getParam("cp2_trace_level", cp2_trace_level)) {
    cp2_evidence_mode = true;
    if (kaist_vio_exact_header_stereo) {
      PRINT_ERROR(RED "[SERIAL-KAIST]: exact-header mode cannot be combined with CP2 evidence mode\n" RESET);
      ros::shutdown();
      return EXIT_FAILURE;
    }
    std::string cp2_context_path;
    std::string cp2_trace_directory;
    std::string cp2_sequence_id;
    int cp2_sequence_index = -1;
    bool cp2_shadow_enabled = false;
    if (!nh->getParam("cp2_context_path", cp2_context_path) ||
        !nh->getParam("cp2_trace_directory", cp2_trace_directory) ||
        !nh->getParam("cp2_sequence_id", cp2_sequence_id) ||
        !nh->getParam("cp2_sequence_index", cp2_sequence_index) ||
        !nh->getParam("cp2_shadow_enabled", cp2_shadow_enabled) ||
        cp2_sequence_index < 0) {
      PRINT_ERROR(RED "[SERIAL-CP2]: incomplete runtime identity parameters\n" RESET);
      ros::shutdown();
      return EXIT_FAILURE;
    }
    CP2RuntimeContextExpectation expected;
    expected.sequence_index = static_cast<std::uint64_t>(cp2_sequence_index);
    expected.sequence_id = cp2_sequence_id;
    expected.mode = UpdaterOptions::landmark_elimination_as_string(
        params.msckf_options.landmark_elimination);
    expected.shadow_enabled = cp2_shadow_enabled;
    expected.trace_level = cp2_trace_level;
    expected.trace_directory = cp2_trace_directory;
    if (cp2_trace_level == "recorded_full") {
      expected.checkpoint = "CP2-C";
    } else if (cp2_trace_level == "sequence") {
      expected.checkpoint = "CP2-D";
    } else if (cp2_trace_level == "timing") {
      expected.checkpoint = "CP2-E";
    } else {
      PRINT_ERROR(RED "[SERIAL-CP2]: unsupported trace level\n" RESET);
      ros::shutdown();
      return EXIT_FAILURE;
    }
    const CP2RuntimeContextResult parsed =
        ReadCP2RuntimeContextFile(cp2_context_path, expected);
    if (!parsed.accepted()) {
      PRINT_ERROR(RED "[SERIAL-CP2]: runtime context rejected: %s\n" RESET,
                  cp2_runtime_context_status_name(parsed.status));
      ros::shutdown();
      return EXIT_FAILURE;
    }
    cp2_context = parsed.context;
    if (!read_cp2_output_capabilities(*nh, cp2_output_capabilities) ||
        !ValidateCP2RuntimeOutputCapabilities(
            cp2_context, cp2_output_capabilities)) {
      PRINT_ERROR(RED "[SERIAL-CP2]: held output capability population is invalid\n" RESET);
      ros::shutdown();
      return EXIT_FAILURE;
    }
    // The timing trace implementation is deliberately reviewable before the
    // final CP2-E profile is frozen, but no direct executable invocation may
    // cross the recorded-input boundary yet.  Keep this second, in-binary
    // lock in addition to the Python runner's pre-access lock.  The lock may
    // only be replaced by an approval-bound capability check at the final
    // clean source identity after the privileged apply/validate/restore
    // rehearsal succeeds.
    if (cp2_context.trace_level == CP2RuntimeTraceLevel::kTiming) {
      PRINT_ERROR(RED "[SERIAL-CP2]: CP2-E is blocked before bag access pending the frozen timing profile, privilege rehearsal, and formal gate\n" RESET);
      ros::shutdown();
      return EXIT_FAILURE;
    }
    bool save_total_state = false;
    nh->param<bool>("save_total_state", save_total_state, false);
    const bool sequence_outputs = cp2_trace_level == "sequence";
    if (save_total_state != sequence_outputs ||
        params.record_timing_information != sequence_outputs) {
      PRINT_ERROR(RED "[SERIAL-CP2]: legacy output Boolean combination differs from trace level\n" RESET);
      ros::shutdown();
      return EXIT_FAILURE;
    }
    if (sequence_outputs) {
      std::string filepath_est;
      std::string filepath_std;
      std::string expected_filepath_est;
      std::string expected_filepath_std;
      std::string expected_timing_path;
      nh->param<std::string>("filepath_est", filepath_est, "");
      nh->param<std::string>("filepath_std", filepath_std, "");
      if (!cp2_context.legacy_state_path.available ||
          !cp2_context.legacy_deviation_path.available ||
          !cp2_context.legacy_timing_path.available ||
          !CP2OutputCapabilityPath(cp2_output_capabilities.legacy_state,
                                   expected_filepath_est) ||
          !CP2OutputCapabilityPath(cp2_output_capabilities.legacy_deviation,
                                   expected_filepath_std) ||
          !CP2OutputCapabilityPath(cp2_output_capabilities.legacy_timing,
                                   expected_timing_path) ||
          !ValidateCP2OutputCapabilityBinding(
              cp2_context.legacy_state_path.value,
              cp2_output_capabilities.legacy_state) ||
          !ValidateCP2OutputCapabilityBinding(
              cp2_context.legacy_deviation_path.value,
              cp2_output_capabilities.legacy_deviation) ||
          !ValidateCP2OutputCapabilityBinding(
              cp2_context.legacy_timing_path.value,
              cp2_output_capabilities.legacy_timing) ||
          filepath_est != expected_filepath_est ||
          filepath_std != expected_filepath_std ||
          params.record_timing_filepath != expected_timing_path) {
        PRINT_ERROR(RED "[SERIAL-CP2]: legacy outputs do not use held inode capabilities\n" RESET);
        ros::shutdown();
        return EXIT_FAILURE;
      }
      // This field is not parser-controlled. Set it only after all three
      // exact held-capability paths and their required launch Booleans pass.
      params.cp2_preopened_output_mode = true;
      params.cp2_legacy_state_capability =
          cp2_output_capabilities.legacy_state;
      params.cp2_legacy_deviation_capability =
          cp2_output_capabilities.legacy_deviation;
      params.cp2_legacy_timing_capability =
          cp2_output_capabilities.legacy_timing;
      params.cp2_legacy_state_canonical_path =
          cp2_context.legacy_state_path.value;
      params.cp2_legacy_deviation_canonical_path =
          cp2_context.legacy_deviation_path.value;
      params.cp2_legacy_timing_canonical_path =
          cp2_context.legacy_timing_path.value;
    }
    if (nh->hasParam("path_gt")) {
      PRINT_ERROR(RED "[SERIAL-CP2]: ground-truth initialization parameter is forbidden\n" RESET);
      ros::shutdown();
      return EXIT_FAILURE;
    }
    if (params.state_options.num_cameras != 2 || topic_cameras.size() != 2U ||
        topic_cameras[0] == topic_cameras[1]) {
      PRINT_ERROR(RED "[SERIAL-CP2]: evidence requires two distinct camera topics\n" RESET);
      ros::shutdown();
      return EXIT_FAILURE;
    }
  }

  // Ensure every YAML/external parameter required above was loaded before
  // estimator or output construction.
  if (!parser->successful()) {
    PRINT_ERROR(RED "[SERIAL]: unable to parse all parameters, please fix\n" RESET);
    ros::shutdown();
    return EXIT_FAILURE;
  }

  if (kaist_vio_exact_header_stereo && nh->hasParam("path_gt")) {
    PRINT_ERROR(RED "[SERIAL-KAIST]: runtime ground truth is forbidden\n" RESET);
    ros::shutdown();
    return EXIT_FAILURE;
  }

  // Load optional ordinary-run ground truth only after the CP2 context gate
  // has ruled evidence mode out.  A CP2 launch therefore cannot cause even a
  // failed ground-truth pathname lookup before its explicit prohibition is
  // enforced above.
  std::map<double, Eigen::Matrix<double, 17, 1>> gt_states;
  if (!cp2_evidence_mode && nh->hasParam("path_gt")) {
    std::string path_to_gt;
    nh->param<std::string>("path_gt", path_to_gt, "");
    if (!path_to_gt.empty()) {
      ov_core::DatasetReader::load_gt_file(path_to_gt, gt_states);
      PRINT_DEBUG("[SERIAL]: gt file path is: %s\n", path_to_gt.c_str());
    }
  }

  auto sys = std::make_shared<VioManager>(params);
  auto viz = std::make_shared<ROS1Visualizer>(nh, sys);

  // The runtime parameter population is captured by this executable—not
  // inferred from the prelaunch dump—and compared in canonical typed bytes
  // before a bag pathname is retrieved or a rosbag object is constructed.
  if (cp2_evidence_mode) {
    const CP2ROS1ParameterCapture runtime_parameters =
        CaptureCP2ROS1ResolvedParameters();
    if (!runtime_parameters.accepted() ||
        runtime_parameters.canonical_sha256 !=
            cp2_context.resolved_parameters_sha256 ||
        !WriteCP2OutputCapability(
            cp2_context.runtime_parameters_path.value,
            cp2_output_capabilities.runtime_parameters,
            runtime_parameters.raw_json)) {
      PRINT_ERROR(RED "[SERIAL-CP2]: runtime parameter capture/binding failed: %s\n" RESET,
                  cp2_ros1_parameter_status_name(runtime_parameters.status));
      ros::shutdown();
      return EXIT_FAILURE;
    }
  }

  // Capture the complete loaded-object set after construction-time ROS/plugin
  // loading but immediately before the bag path is retrieved.  This is the
  // stable pre-run boundary compared with the post-run map; the output is
  // capability-bound and fsynced, and any failure remains pre-bag.
  if (cp2_evidence_mode) {
    std::string loader_map_before;
    if (!CP2SerialRuntimeTrace::ReadLoaderMap(loader_map_before) ||
        !WriteCP2OutputCapability(
            cp2_context.loader_map_before_path.value,
            cp2_output_capabilities.loader_map_before,
            loader_map_before)) {
      PRINT_ERROR(RED "[SERIAL-CP2]: unable to seal pre-run loader map\n" RESET);
      ros::shutdown();
      return EXIT_FAILURE;
    }
  }

  // Location of the ROS bag.  Evidence mode deliberately reaches this
  // parameter only after the context and live resolved-parameter digest pass.
  std::string path_to_bag;
  nh->param<std::string>("path_bag", path_to_bag,
                         "/home/patrick/datasets/eth/V1_01_easy.bag");
  PRINT_DEBUG("[SERIAL]: ros bag path is: %s\n", path_to_bag.c_str());

  //===================================================================================
  //===================================================================================
  //===================================================================================

  // Load rosbag here, and find messages we can play
  rosbag::Bag bag;
  bag.open(path_to_bag, rosbag::bagmode::Read);

  // We should load the bag as a view
  // Here we go from beginning of the bag to the end of the bag
  rosbag::View view_full;
  rosbag::View view;

  // Start a few seconds in from the full view time
  // If we have a negative duration then use the full bag length
  view_full.addQuery(bag);
  ros::Time time_init = view_full.getBeginTime();
  time_init += ros::Duration(bag_start);
  ros::Time time_finish = (bag_durr < 0) ? view_full.getEndTime() : time_init + ros::Duration(bag_durr);
  PRINT_DEBUG("time start = %.6f\n", time_init.toSec());
  PRINT_DEBUG("time end   = %.6f\n", time_finish.toSec());
  view.addQuery(bag, time_init, time_finish);

  // Check to make sure we have data to play
  if (view.size() == 0) {
    PRINT_ERROR(RED "[SERIAL]: No messages to play on specified topics.  Exiting.\n" RESET);
    ros::shutdown();
    return EXIT_FAILURE;
  }

  // We going to loop through and collect a list of all messages
  // This is done so we can access arbitrary points in the bag
  // NOTE: if we instantiate messages here, this requires the whole bag to be read
  // NOTE: thus we just check the topic which allows us to quickly loop through the index
  // NOTE: see this PR https://github.com/ros/ros_comm/issues/117
  double max_camera_time = -1;
  std::vector<rosbag::MessageInstance> msgs;
  for (const rosbag::MessageInstance &msg : view) {
    if (!ros::ok())
      break;
    if (msg.getTopic() == topic_imu) {
      // if (msg.instantiate<sensor_msgs::Imu>() == nullptr) {
      //   PRINT_ERROR(RED "[SERIAL]: IMU topic has unmatched message types!!\n" RESET);
      //   PRINT_ERROR(RED "[SERIAL]: Supports: sensor_msgs::Imu\n" RESET);
      //   return EXIT_FAILURE;
      // }
      msgs.push_back(msg);
    }
    for (int i = 0; i < params.state_options.num_cameras; i++) {
      if (msg.getTopic() == topic_cameras.at(i)) {
        // sensor_msgs::CompressedImage::ConstPtr img_c = msg.instantiate<sensor_msgs::CompressedImage>();
        // sensor_msgs::Image::ConstPtr img_i = msg.instantiate<sensor_msgs::Image>();
        // if (img_c == nullptr && img_i == nullptr) {
        //   PRINT_ERROR(RED "[SERIAL]: Image topic has unmatched message types!!\n" RESET);
        //   PRINT_ERROR(RED "[SERIAL]: Supports: sensor_msgs::Image and sensor_msgs::CompressedImage\n" RESET);
        //   return EXIT_FAILURE;
        // }
        msgs.push_back(msg);
        max_camera_time = std::max(max_camera_time, msg.getTime().toSec());
      }
    }
  }
  PRINT_DEBUG("[SERIAL]: total of %zu messages!\n", msgs.size());

  std::shared_ptr<CP2SerialRuntimeTrace> cp2_runtime_trace;
  CP2OpenedOutput cp2_journal_output;
  std::shared_ptr<CP2TraceJournalSink> cp2_journal_sink;
  std::map<std::size_t, CP2SerialPair> cp2_pair_by_anchor;
  std::map<std::size_t, KaistVioSerialPair> kaist_pair_by_anchor;
  std::uint64_t kaist_queued_pairs = 0U;
  std::uint64_t kaist_frequency_thinned_pairs = 0U;
  std::uint64_t kaist_cam0_decode_failures = 0U;
  std::uint64_t kaist_cam1_decode_failures = 0U;
  if (cp2_evidence_mode) {
    std::vector<CP2SerialFilteredMessage> filtered;
    try {
      filtered.reserve(msgs.size());
      for (const rosbag::MessageInstance &instance : msgs) {
        CP2SerialFilteredMessage metadata;
        if (!cp2_time_nanoseconds(instance.getTime(), metadata.record_time_ns)) {
          throw std::overflow_error("rosbag record timestamp is invalid");
        }
        if (instance.getTopic() == topic_imu) {
          metadata.kind = CP2SerialMessageKind::kImu;
        } else if (instance.getTopic() == topic_cameras[0]) {
          metadata.kind = CP2SerialMessageKind::kCamera0;
          const sensor_msgs::Image::ConstPtr image =
              instance.instantiate<sensor_msgs::Image>();
          if (!image || !cp2_time_nanoseconds(image->header.stamp,
                                               metadata.header_time_ns)) {
            throw std::runtime_error("cam0 image/header timestamp is invalid");
          }
        } else if (instance.getTopic() == topic_cameras[1]) {
          metadata.kind = CP2SerialMessageKind::kCamera1;
          const sensor_msgs::Image::ConstPtr image =
              instance.instantiate<sensor_msgs::Image>();
          if (!image || !cp2_time_nanoseconds(image->header.stamp,
                                               metadata.header_time_ns)) {
            throw std::runtime_error("cam1 image/header timestamp is invalid");
          }
        } else {
          throw std::logic_error("topic-filtered serial view contains an unknown topic");
        }
        filtered.push_back(metadata);
      }
    } catch (const std::exception &error) {
      PRINT_ERROR(RED "[SERIAL-CP2]: unable to form exact filtered view: %s\n" RESET,
                  error.what());
      ros::shutdown();
      return EXIT_FAILURE;
    }

    CP2SerialPairingResult selected = CP2SerialPairSelector::Select(
        cp2_context.sequence_index, filtered);
    if (!selected.accepted()) {
      PRINT_ERROR(RED "[SERIAL-CP2]: exact pair selection failed: %s\n" RESET,
                  cp2_serial_pairing_status_name(selected.status));
      ros::shutdown();
      return EXIT_FAILURE;
    }
    try {
      for (const CP2SerialPair &pair : selected.pairs) {
        const std::size_t anchor =
            static_cast<std::size_t>(pair.anchor_filtered_index);
        if (static_cast<std::uint64_t>(anchor) !=
                pair.anchor_filtered_index ||
            !cp2_pair_by_anchor.emplace(anchor, pair).second) {
          throw std::runtime_error("selected pair anchor is not unique/representable");
        }
      }
      cp2_runtime_trace = std::make_shared<CP2SerialRuntimeTrace>(
          cp2_context, cp2_output_capabilities,
          std::move(selected.pairs));
    } catch (const std::exception &error) {
      PRINT_ERROR(RED "[SERIAL-CP2]: serial trace initialization failed: %s\n" RESET,
                  error.what());
      ros::shutdown();
      return EXIT_FAILURE;
    }
    if (!cp2_runtime_trace->ready()) {
      PRINT_ERROR(RED "[SERIAL-CP2]: serial trace population rejected: %s\n" RESET,
                  cp2_runtime_trace->failure().c_str());
      ros::shutdown();
      return EXIT_FAILURE;
    }

    if (!sys->set_cp2_update_callback(
            [cp2_runtime_trace](CP2LiveUpdateEvent event) {
              (void)cp2_runtime_trace->NoteUpdate(event);
            },
            false)) {
      PRINT_ERROR(RED "[SERIAL-CP2]: updater observer installation failed\n" RESET);
      ros::shutdown();
      return EXIT_FAILURE;
    }
    if (!viz->set_cp2_serial_processing_observer(
            [cp2_runtime_trace, sys](const CP2SerialProcessingEvent &event) {
              return cp2_runtime_trace->NoteProcessing(
                  event.context, event.processing_entered,
                  event.processing_returned, event.updater_invoked,
                  event.state_row_emitted, event.position_G,
                  event.quaternion_ItoG_xyzw,
                  sys->cp2_trace_fatal_latched());
            })) {
      PRINT_ERROR(RED "[SERIAL-CP2]: processing observer installation failed\n" RESET);
      ros::shutdown();
      return EXIT_FAILURE;
    }

    if (cp2_context.trace_level == CP2RuntimeTraceLevel::kRecordedFull) {
      if (!OpenCP2OutputCapability(
              cp2_context.updater_trace_path.value,
              cp2_output_capabilities.updater_trace,
              cp2_journal_output) ||
          !CP2TraceJournalSink::CreateForPreopenedFile(
              cp2_journal_output.descriptor(), cp2_journal_sink)) {
        PRINT_ERROR(RED "[SERIAL-CP2]: authoritative journal creation failed\n" RESET);
        ros::shutdown();
        return EXIT_FAILURE;
      }
      if (!sys->set_cp2_recorded_sink(cp2_journal_sink)) {
        PRINT_ERROR(RED "[SERIAL-CP2]: authoritative sink installation failed\n" RESET);
        ros::shutdown();
        return EXIT_FAILURE;
      }
    }
  }

  if (kaist_vio_exact_header_stereo) {
    std::vector<KaistVioSerialMessage> filtered;
    try {
      filtered.reserve(msgs.size());
      for (const rosbag::MessageInstance &instance : msgs) {
        KaistVioSerialMessage metadata;
        if (!cp2_time_nanoseconds(instance.getTime(),
                                  metadata.record_time_ns)) {
          throw std::overflow_error("rosbag record timestamp is invalid");
        }
        if (instance.getTopic() == topic_imu) {
          metadata.kind = KaistVioSerialMessageKind::kImu;
        } else if (instance.getTopic() == topic_cameras[0]) {
          metadata.kind = KaistVioSerialMessageKind::kCamera0;
          const sensor_msgs::Image::ConstPtr image =
              instance.instantiate<sensor_msgs::Image>();
          if (!image || !cp2_time_nanoseconds(image->header.stamp,
                                               metadata.header_time_ns)) {
            throw std::runtime_error("cam0 image/header timestamp is invalid");
          }
        } else if (instance.getTopic() == topic_cameras[1]) {
          metadata.kind = KaistVioSerialMessageKind::kCamera1;
          const sensor_msgs::Image::ConstPtr image =
              instance.instantiate<sensor_msgs::Image>();
          if (!image || !cp2_time_nanoseconds(image->header.stamp,
                                               metadata.header_time_ns)) {
            throw std::runtime_error("cam1 image/header timestamp is invalid");
          }
        } else {
          throw std::logic_error(
              "topic-filtered serial view contains an unknown topic");
        }
        filtered.push_back(metadata);
      }
    } catch (const std::exception &error) {
      PRINT_ERROR(RED "[SERIAL-KAIST]: unable to form exact filtered view: %s\n" RESET,
                  error.what());
      ros::shutdown();
      return EXIT_FAILURE;
    }

    const KaistVioSerialPairingResult selected =
        KaistVioSerialPairSelector::Select(filtered);
    if (!selected.accepted() || selected.pairs.empty()) {
      PRINT_ERROR(RED "[SERIAL-KAIST]: exact-header pair selection failed: %s\n" RESET,
                  kaist_vio_serial_pairing_status_name(selected.status));
      ros::shutdown();
      return EXIT_FAILURE;
    }
    try {
      for (const KaistVioSerialPair &pair : selected.pairs) {
        const std::size_t anchor =
            static_cast<std::size_t>(pair.anchor_filtered_index);
        if (static_cast<std::uint64_t>(anchor) !=
                pair.anchor_filtered_index ||
            !kaist_pair_by_anchor.emplace(anchor, pair).second) {
          throw std::runtime_error(
              "selected exact-header anchor is not unique/representable");
        }
      }
    } catch (const std::exception &error) {
      PRINT_ERROR(RED "[SERIAL-KAIST]: exact-header pair map failed: %s\n" RESET,
                  error.what());
      ros::shutdown();
      return EXIT_FAILURE;
    }
    PRINT_INFO("[SERIAL-KAIST]: exact_header_pairs=%zu camera0_without_match=%llu camera1_without_match=%llu record_delta_ge_20ms=%llu maximum_record_delta_ns=%llu\n",
               selected.pairs.size(),
               static_cast<unsigned long long>(selected.camera0_without_match),
               static_cast<unsigned long long>(selected.camera1_without_match),
               static_cast<unsigned long long>(selected.record_delta_at_or_above_20ms),
               static_cast<unsigned long long>(selected.maximum_record_delta_ns));
  }

  //===================================================================================
  //===================================================================================
  //===================================================================================

  // Loop through our message array, and lets process them
  std::set<int> used_index;
  for (int m = 0; m < (int)msgs.size(); m++) {

    // End once we reach the last time, or skip if before beginning time (shouldn't happen)
    if (cp2_serial_should_stop_iteration(
            ros::ok(), msgs.at(m).getTime() > time_finish,
            cp2_evidence_mode || kaist_vio_exact_header_stereo,
            msgs.at(m).getTime().toSec() > max_camera_time))
      break;
    if (msgs.at(m).getTime() < time_init)
      continue;

    // Skip messages that we have already used
    if (used_index.find(m) != used_index.end()) {
      used_index.erase(m);
      continue;
    }

    // IMU processing
    if (msgs.at(m).getTopic() == topic_imu) {
      // PRINT_DEBUG("processing imu = %.3f sec\n", msgs.at(m).getTime().toSec() - time_init.toSec());
      const sensor_msgs::Imu::ConstPtr imu =
          msgs.at(m).instantiate<sensor_msgs::Imu>();
      if (cp2_evidence_mode) {
        if (!imu) {
          PRINT_ERROR(RED "[SERIAL-CP2]: filtered IMU message has wrong type\n" RESET);
          ros::shutdown();
          return EXIT_FAILURE;
        }
        try {
          viz->callback_inertial(imu);
        } catch (const std::exception &error) {
          PRINT_ERROR(RED "[SERIAL-CP2]: camera processing terminated: %s\n" RESET,
                      error.what());
          ros::shutdown();
          return EXIT_FAILURE;
        }
        if (!cp2_runtime_trace->ready() ||
            sys->cp2_trace_fatal_latched()) {
          PRINT_ERROR(RED "[SERIAL-CP2]: trace invariant failed after IMU dispatch\n" RESET);
          ros::shutdown();
          return EXIT_FAILURE;
        }
      } else {
        viz->callback_inertial(imu);
      }
    }

    // CP2 uses the precomputed exact integer first-forward selection.  Only a
    // selected anchor enters the camera callback; candidate and rejected
    // camera messages are otherwise skipped without re-running a float-time
    // nearest-neighbor search.
    if (cp2_evidence_mode) {
      const auto selected = cp2_pair_by_anchor.find(
          static_cast<std::size_t>(m));
      if (selected == cp2_pair_by_anchor.end()) {
        continue;
      }
      const CP2SerialPair &pair = selected->second;
      const std::size_t cam0_index =
          static_cast<std::size_t>(pair.cam0_filtered_index);
      const std::size_t cam1_index =
          static_cast<std::size_t>(pair.cam1_filtered_index);
      if (cam0_index >= msgs.size() || cam1_index >= msgs.size() ||
          msgs[cam0_index].getTopic() != topic_cameras[0] ||
          msgs[cam1_index].getTopic() != topic_cameras[1]) {
        PRINT_ERROR(RED "[SERIAL-CP2]: retained pair/source join failed\n" RESET);
        ros::shutdown();
        return EXIT_FAILURE;
      }
      const sensor_msgs::Image::ConstPtr image0 =
          msgs[cam0_index].instantiate<sensor_msgs::Image>();
      const sensor_msgs::Image::ConstPtr image1 =
          msgs[cam1_index].instantiate<sensor_msgs::Image>();
      if (!image0 || !image1) {
        PRINT_ERROR(RED "[SERIAL-CP2]: retained pair image type changed\n" RESET);
        ros::shutdown();
        return EXIT_FAILURE;
      }
      CP2UpdateInvocationContext invocation;
      invocation.sequence_index = pair.sequence_index;
      invocation.pair_index = pair.pair_index;
      invocation.camera_timestamp_ns = pair.camera_timestamp_ns;
      try {
        const CP2SerialEnqueueStatus status = viz->callback_stereo_cp2(
            image0, image1, 0, 1, invocation);
        if (!cp2_runtime_trace->NoteEnqueue(
                pair.pair_index, cp2_runtime_enqueue_status(status))) {
          throw std::runtime_error(cp2_runtime_trace->failure());
        }
      } catch (const std::exception &error) {
        PRINT_ERROR(RED "[SERIAL-CP2]: camera enqueue terminated: %s\n" RESET,
                    error.what());
        ros::shutdown();
        return EXIT_FAILURE;
      }
      continue;
    }

    // The KAIST path is mutually exclusive with CP2 and the unchanged legacy
    // record-time branch below. Only an earlier-record anchor enters the callback;
    // unmatched camera messages remain visible in the preserved bag and the
    // exact selection summary above.
    if (kaist_vio_exact_header_stereo) {
      const auto selected =
          kaist_pair_by_anchor.find(static_cast<std::size_t>(m));
      if (selected == kaist_pair_by_anchor.end()) {
        continue;
      }
      const KaistVioSerialPair &pair = selected->second;
      const std::size_t cam0_index =
          static_cast<std::size_t>(pair.cam0_filtered_index);
      const std::size_t cam1_index =
          static_cast<std::size_t>(pair.cam1_filtered_index);
      if (cam0_index >= msgs.size() || cam1_index >= msgs.size() ||
          msgs[cam0_index].getTopic() != topic_cameras[0] ||
          msgs[cam1_index].getTopic() != topic_cameras[1]) {
        PRINT_ERROR(RED "[SERIAL-KAIST]: exact-header pair/source join failed\n" RESET);
        ros::shutdown();
        return EXIT_FAILURE;
      }
      const sensor_msgs::Image::ConstPtr image0 =
          msgs[cam0_index].instantiate<sensor_msgs::Image>();
      const sensor_msgs::Image::ConstPtr image1 =
          msgs[cam1_index].instantiate<sensor_msgs::Image>();
      std::uint64_t image0_stamp_ns = 0U;
      std::uint64_t image1_stamp_ns = 0U;
      if (!image0 || !image1 ||
          !cp2_time_nanoseconds(image0->header.stamp, image0_stamp_ns) ||
          !cp2_time_nanoseconds(image1->header.stamp, image1_stamp_ns) ||
          image0_stamp_ns != pair.camera_timestamp_ns ||
          image1_stamp_ns != pair.camera_timestamp_ns) {
        PRINT_ERROR(RED "[SERIAL-KAIST]: exact-header pair identity changed\n" RESET);
        ros::shutdown();
        return EXIT_FAILURE;
      }
      try {
        const CP2SerialEnqueueStatus status =
            viz->callback_stereo_serial(image0, image1, 0, 1);
        switch (status) {
        case CP2SerialEnqueueStatus::kQueued:
          ++kaist_queued_pairs;
          break;
        case CP2SerialEnqueueStatus::kFrequencyDropped:
          ++kaist_frequency_thinned_pairs;
          break;
        case CP2SerialEnqueueStatus::kCam0DecodeFailed:
          ++kaist_cam0_decode_failures;
          throw std::runtime_error("cam0 decode failed");
        case CP2SerialEnqueueStatus::kCam1DecodeFailed:
          ++kaist_cam1_decode_failures;
          throw std::runtime_error("cam1 decode failed");
        }
      } catch (const std::exception &error) {
        PRINT_ERROR(RED "[SERIAL-KAIST]: camera processing terminated: %s\n" RESET,
                    error.what());
        ros::shutdown();
        return EXIT_FAILURE;
      }
      continue;
    }

    // Camera processing
    for (int cam_id = 0; cam_id < params.state_options.num_cameras; cam_id++) {

      // Skip if this message is not a camera topic
      if (msgs.at(m).getTopic() != topic_cameras.at(cam_id))
        continue;

      // We have a matching camera topic here, now find the other cameras for this time
      // For each camera, we will find the nearest timestamp (within 0.02sec) that is greater than the current
      // If we are unable, then this message should just be skipped since it isn't a sync'ed pair!
      std::map<int, int> camid_to_msg_index;
      double meas_time = msgs.at(m).getTime().toSec();
      for (int cam_idt = 0; cam_idt < params.state_options.num_cameras; cam_idt++) {
        if (cam_idt == cam_id) {
          camid_to_msg_index.insert({cam_id, m});
          continue;
        }
        int cam_idt_idx = -1;
        for (int mt = m; mt < (int)msgs.size(); mt++) {
          if (msgs.at(mt).getTopic() != topic_cameras.at(cam_idt))
            continue;
          if (std::abs(msgs.at(mt).getTime().toSec() - meas_time) < 0.02)
            cam_idt_idx = mt;
          break;
        }
        if (cam_idt_idx != -1) {
          camid_to_msg_index.insert({cam_idt, cam_idt_idx});
        }
      }

      // Skip processing if we were unable to find any messages
      if ((int)camid_to_msg_index.size() != params.state_options.num_cameras) {
        PRINT_DEBUG(YELLOW "[SERIAL]: Unable to find stereo pair for message %d at %.2f into bag (will skip!)\n" RESET, m,
                    meas_time - time_init.toSec());
        continue;
      }

      // Check if we should initialize using the groundtruth
      Eigen::Matrix<double, 17, 1> imustate;
      if (!gt_states.empty() && !sys->initialized() && ov_core::DatasetReader::get_gt_state(meas_time, imustate, gt_states)) {
        // biases are pretty bad normally, so zero them
        // imustate.block(11,0,6,1).setZero();
        sys->initialize_with_gt(imustate);
      }

      // Pass our data into our visualizer callbacks!
      // PRINT_DEBUG("processing cam = %.3f sec\n", msgs.at(m).getTime().toSec() - time_init.toSec());
      if (params.state_options.num_cameras == 1) {
        viz->callback_monocular(msgs.at(camid_to_msg_index.at(0)).instantiate<sensor_msgs::Image>(), 0);
      } else if (params.state_options.num_cameras == 2) {
        auto msg0 = msgs.at(camid_to_msg_index.at(0));
        auto msg1 = msgs.at(camid_to_msg_index.at(1));
        used_index.insert(camid_to_msg_index.at(0)); // skip this message
        used_index.insert(camid_to_msg_index.at(1)); // skip this message
        viz->callback_stereo(msg0.instantiate<sensor_msgs::Image>(), msg1.instantiate<sensor_msgs::Image>(), 0, 1);
      } else {
        PRINT_ERROR(RED "[SERIAL]: We currently only support 1 or 2 camera serial input....\n" RESET);
        return EXIT_FAILURE;
      }

      break;
    }
  }

  std::uint64_t kaist_processed_pairs = 0U;
  std::uint64_t kaist_pending_pairs = 0U;
  if (kaist_vio_exact_header_stereo) {
    const ROS1SerialCameraQueueState queue_state =
        viz->serial_camera_queue_state();
    kaist_pending_pairs =
        static_cast<std::uint64_t>(queue_state.pending_messages);
    if (static_cast<std::size_t>(kaist_pending_pairs) !=
            queue_state.pending_messages ||
        queue_state.processing_active || kaist_pending_pairs != 0U) {
      PRINT_ERROR(RED "[SERIAL-KAIST]: camera queue did not drain: pending_pairs=%llu processing_active=%d\n" RESET,
                  static_cast<unsigned long long>(kaist_pending_pairs),
                  static_cast<int>(queue_state.processing_active));
      ros::shutdown();
      return EXIT_FAILURE;
    }
    kaist_processed_pairs = kaist_queued_pairs;
  }

  // Final visualization
  viz->visualize_final();

  if (kaist_vio_exact_header_stereo) {
    PRINT_INFO("[SERIAL-KAIST]: queued_pairs=%llu processed_pairs=%llu frequency_thinned_pairs=%llu cam0_decode_failures=%llu cam1_decode_failures=%llu pending_pairs=%llu\n",
               static_cast<unsigned long long>(kaist_queued_pairs),
               static_cast<unsigned long long>(kaist_processed_pairs),
               static_cast<unsigned long long>(kaist_frequency_thinned_pairs),
               static_cast<unsigned long long>(kaist_cam0_decode_failures),
               static_cast<unsigned long long>(kaist_cam1_decode_failures),
               static_cast<unsigned long long>(kaist_pending_pairs));
  }

  // Passive T0 publication is deliberately non-authoritative: a sink failure
  // is reported but cannot change replay completion or any estimator result.
  if (!sys->finalize_turnsafe_t0()) {
    PRINT_WARNING(YELLOW
                  "[TURNSAFE-T0]: status=finalize_failed "
                  "baseline_unchanged=1\n" RESET);
  }

  if (cp2_evidence_mode) {
    if (sys->cp2_trace_fatal_latched() || !cp2_runtime_trace->ready()) {
      PRINT_ERROR(RED "[SERIAL-CP2]: run ended with an incomplete trace\n" RESET);
      ros::shutdown();
      return EXIT_FAILURE;
    }
    if (cp2_journal_sink) {
      if (!cp2_journal_sink->Finalize() ||
          !SealCP2OutputCapability(cp2_journal_output,
                                   cp2_journal_sink->bytes_written())) {
        PRINT_ERROR(RED "[SERIAL-CP2]: authoritative journal durability failed: %s\n" RESET,
                    cp2_trace_journal_failure_name(cp2_journal_sink->failure()));
        ros::shutdown();
        return EXIT_FAILURE;
      }
    }
    if (!cp2_runtime_trace->Finalize()) {
      PRINT_ERROR(RED "[SERIAL-CP2]: serial trace finalization failed: %s\n" RESET,
                  cp2_runtime_trace->failure().c_str());
      ros::shutdown();
      return EXIT_FAILURE;
    }
    std::string loader_map_after;
    if (!CP2SerialRuntimeTrace::ReadLoaderMap(loader_map_after) ||
        !WriteCP2OutputCapability(
            cp2_context.loader_map_after_path.value,
            cp2_output_capabilities.loader_map_after,
            loader_map_after)) {
      PRINT_ERROR(RED "[SERIAL-CP2]: unable to seal post-run loader map\n" RESET);
      ros::shutdown();
      return EXIT_FAILURE;
    }
  }

  ros::shutdown();

  // Destroy plugin-owning ROS objects before process-static class loaders.
  viz.reset();
  sys.reset();
  nh.reset();

  // Done!
  return EXIT_SUCCESS;
}
