/*
 * SchurVIO-Lite CP2 serial runtime trace tests.
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include "update/CP2SerialRuntimeTrace.h"
#include "update/CP2TimingClock.h"

#include <gtest/gtest.h>

#include <algorithm>
#include <array>
#include <cstdio>
#include <fcntl.h>
#include <fstream>
#include <iterator>
#include <limits>
#include <stdexcept>
#include <string>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>
#include <vector>

namespace {

struct TemporaryDirectory {
  TemporaryDirectory() {
    std::array<char, 96U> pattern{};
    const std::string value = "/tmp/schurvio-lite-cp2-serial-trace-XXXXXX";
    std::copy(value.begin(), value.end(), pattern.begin());
    char *created = ::mkdtemp(pattern.data());
    if (created != nullptr) {
      path = created;
    }
  }
  ~TemporaryDirectory() {
    for (const std::string &name : names) {
      (void)::unlink((path + "/" + name).c_str());
    }
    if (!path.empty()) {
      (void)::rmdir(path.c_str());
    }
  }
  std::string Add(const std::string &name) {
    names.push_back(name);
    return path + "/" + name;
  }
  std::string path;
  std::vector<std::string> names;
};

struct HeldDescriptors {
  ~HeldDescriptors() {
    for (int descriptor : values) {
      if (descriptor >= 0) {
        (void)::close(descriptor);
      }
    }
  }
  std::vector<int> values;
};

std::uint64_t stat_time_ns(const struct timespec &value) {
  if (value.tv_sec < 0 || value.tv_nsec < 0 ||
      value.tv_nsec >= 1000000000L) {
    throw std::runtime_error("capability timestamp is invalid");
  }
  return static_cast<std::uint64_t>(value.tv_sec) * UINT64_C(1000000000) +
         static_cast<std::uint64_t>(value.tv_nsec);
}

ov_msckf::CP2OutputCapability create_capability(
    const std::string &path, HeldDescriptors &held) {
  const int creator = ::open(path.c_str(),
                             O_RDWR | O_CREAT | O_EXCL | O_CLOEXEC |
                                 O_NOFOLLOW,
                             S_IRUSR | S_IWUSR);
  if (creator < 0) {
    throw std::runtime_error("capability creator open failed");
  }
  const std::string creator_path =
      "/proc/self/fd/" + std::to_string(creator);
  const int descriptor = ::open(creator_path.c_str(), O_RDONLY | O_CLOEXEC);
  const int close_status = ::close(creator);
  if (descriptor < 0 || close_status != 0) {
    if (descriptor >= 0) {
      (void)::close(descriptor);
    }
    throw std::runtime_error("read-only held descriptor creation failed");
  }
  struct stat status {};
  if (::fstat(descriptor, &status) != 0) {
    (void)::close(descriptor);
    throw std::runtime_error("held descriptor stat failed");
  }
  held.values.push_back(descriptor);
  ov_msckf::CP2OutputCapability output;
  output.available = true;
  output.runner_pid = static_cast<std::uint64_t>(::getpid());
  output.descriptor = static_cast<std::uint64_t>(descriptor);
  output.device = static_cast<std::uint64_t>(status.st_dev);
  output.inode = static_cast<std::uint64_t>(status.st_ino);
  output.mode = static_cast<std::uint64_t>(status.st_mode);
  output.link_count = static_cast<std::uint64_t>(status.st_nlink);
  output.uid = static_cast<std::uint64_t>(status.st_uid);
  output.gid = static_cast<std::uint64_t>(status.st_gid);
  output.size = static_cast<std::uint64_t>(status.st_size);
  output.mtime_ns = stat_time_ns(status.st_mtim);
  output.ctime_ns = stat_time_ns(status.st_ctim);
  return output;
}

std::string encode_capability(
    const ov_msckf::CP2OutputCapability &value) {
  return "v1:" + std::to_string(value.runner_pid) + ":" +
         std::to_string(value.descriptor) + ":" +
         std::to_string(value.device) + ":" +
         std::to_string(value.inode) + ":" +
         std::to_string(value.mode) + ":" +
         std::to_string(value.link_count) + ":" +
         std::to_string(value.uid) + ":" +
         std::to_string(value.gid) + ":" +
         std::to_string(value.size) + ":" +
         std::to_string(value.mtime_ns) + ":" +
         std::to_string(value.ctime_ns);
}

std::string read_held(int descriptor, std::size_t size) {
  std::string output(size, '\0');
  std::size_t offset = 0U;
  while (offset < size) {
    const ssize_t count = ::pread(descriptor, &output[offset], size - offset,
                                  static_cast<off_t>(offset));
    if (count < 0 && errno == EINTR) {
      continue;
    }
    if (count <= 0) {
      throw std::runtime_error("held descriptor read failed");
    }
    offset += static_cast<std::size_t>(count);
  }
  return output;
}

std::string read_file(const std::string &path) {
  std::ifstream stream(path, std::ios::binary);
  return std::string(std::istreambuf_iterator<char>(stream),
                     std::istreambuf_iterator<char>());
}

ov_msckf::CP2RuntimeContext context(
    ov_msckf::CP2RuntimeTraceLevel level, TemporaryDirectory &root) {
  ov_msckf::CP2RuntimeContext value;
  value.sequence_index = 0U;
  value.sequence_id = "MH_01_easy";
  value.mode = "nullspace";
  value.trace_level = level;
  value.trace_directory = root.path;
  value.serial_trace_path.available = true;
  value.serial_trace_path.value = root.Add("serial.jsonl");
  value.runtime_parameters_path.available = true;
  value.runtime_parameters_path.value = root.Add("runtime_parameters.json");
  value.loader_map_before_path.available = true;
  value.loader_map_before_path.value = root.Add("loader_before.txt");
  value.loader_map_after_path.available = true;
  value.loader_map_after_path.value = root.Add("loader_after.txt");
  if (level == ov_msckf::CP2RuntimeTraceLevel::kRecordedFull) {
    value.updater_trace_path.available = true;
    value.updater_trace_path.value = root.Add("updater.jsonl");
  } else if (level == ov_msckf::CP2RuntimeTraceLevel::kSequence) {
    value.callback_trace_path.available = true;
    value.callback_trace_path.value = root.Add("callbacks.jsonl");
    value.trajectory_trace_path.available = true;
    value.trajectory_trace_path.value = root.Add("trajectory.jsonl");
    value.legacy_state_path.available = true;
    value.legacy_state_path.value = root.Add("state.txt");
    value.legacy_deviation_path.available = true;
    value.legacy_deviation_path.value = root.Add("deviation.txt");
    value.legacy_timing_path.available = true;
    value.legacy_timing_path.value = root.Add("openvins_timing.csv");
  } else if (level == ov_msckf::CP2RuntimeTraceLevel::kTiming) {
    value.callback_trace_path.available = true;
    value.callback_trace_path.value = root.Add("callbacks.jsonl");
    value.updater_trace_path.available = true;
    value.updater_trace_path.value = root.Add("updater.jsonl");
    value.timing_trace_path.available = true;
    value.timing_trace_path.value = root.Add("timing.jsonl");
  }
  return value;
}

ov_msckf::CP2RuntimeOutputCapabilities capability_population(
    const ov_msckf::CP2RuntimeContext &runtime, HeldDescriptors &held) {
  ov_msckf::CP2RuntimeOutputCapabilities output;
  const auto add = [&held](const ov_msckf::CP2NullablePath &path,
                           ov_msckf::CP2OutputCapability &capability) {
    if (path.available) {
      capability = create_capability(path.value, held);
    }
  };
  add(runtime.serial_trace_path, output.serial_trace);
  add(runtime.callback_trace_path, output.callback_trace);
  add(runtime.trajectory_trace_path, output.trajectory_trace);
  add(runtime.updater_trace_path, output.updater_trace);
  add(runtime.state_payload_path, output.state_payload);
  add(runtime.proposal_payload_path, output.proposal_payload);
  add(runtime.raw_system_payload_path, output.raw_system_payload);
  add(runtime.timing_trace_path, output.timing_trace);
  add(runtime.runtime_parameters_path, output.runtime_parameters);
  add(runtime.loader_map_before_path, output.loader_map_before);
  add(runtime.loader_map_after_path, output.loader_map_after);
  add(runtime.legacy_state_path, output.legacy_state);
  add(runtime.legacy_deviation_path, output.legacy_deviation);
  add(runtime.legacy_timing_path, output.legacy_timing);
  return output;
}

ov_msckf::CP2SerialPair pair(std::uint64_t index,
                             std::uint64_t timestamp) {
  ov_msckf::CP2SerialPair value;
  value.sequence_index = 0U;
  value.pair_index = index;
  value.anchor_filtered_index = 2U * index;
  value.anchor_camera_id = 0U;
  value.cam0_filtered_index = 2U * index;
  value.cam1_filtered_index = 2U * index + 1U;
  value.cam0_record_time_ns = timestamp - 5U;
  value.cam1_record_time_ns = timestamp + 5U;
  value.cam0_header_time_ns = timestamp;
  value.cam1_header_time_ns = timestamp + 1U;
  value.camera_timestamp_ns = timestamp;
  value.absolute_record_delta_ns = 10U;
  return value;
}

ov_msckf::CP2LiveUpdateEvent update(std::uint64_t pair_index,
                                    std::uint64_t invocation_id,
                                    std::uint64_t timestamp) {
  ov_msckf::CP2LiveUpdateEvent value;
  value.invocation_context_available = true;
  value.sequence_index = 0U;
  value.pair_index = pair_index;
  value.invocation_id = invocation_id;
  value.camera_timestamp_ns = timestamp;
  value.timing_endpoint_valid = true;
  value.timing_start_ns = 1000U + invocation_id * 100U;
  value.timing_end_ns = value.timing_start_ns + 25U;
  value.duration_ns = 25U;
  value.terminal_status =
      ov_msckf::CP2UpdateTerminalStatus::kCommittedCounted;
  value.terminal_subreason = ov_msckf::CP2UpdateTerminalSubreason::kNone;
  value.input_feature_count = 2U;
  value.raw_system_count = 1U;
  value.baseline_precompression_system_nonempty = true;
  value.baseline_precompression_rows_available = true;
  value.baseline_precompression_rows = 4U;
  value.baseline_compressed_system_nonempty = true;
  value.baseline_compressed_rows_available = true;
  value.baseline_compressed_rows = 3U;
  value.baseline_preflight_attempted = true;
  value.baseline_preflight_accepted = true;
  value.baseline_commit_occurred = true;
  return value;
}

ov_msckf::CP2LiveUpdateEvent empty_update(std::uint64_t pair_index,
                                          std::uint64_t invocation_id,
                                          std::uint64_t timestamp) {
  ov_msckf::CP2LiveUpdateEvent value =
      update(pair_index, invocation_id, timestamp);
  value.terminal_status = ov_msckf::CP2UpdateTerminalStatus::kEmptyInput;
  value.terminal_subreason =
      ov_msckf::CP2UpdateTerminalSubreason::kInputEmpty;
  value.input_feature_count = 0U;
  value.raw_system_count = 0U;
  value.baseline_precompression_system_nonempty = false;
  value.baseline_precompression_rows_available = false;
  value.baseline_precompression_rows = 0U;
  value.baseline_compressed_system_nonempty = false;
  value.baseline_compressed_rows_available = false;
  value.baseline_compressed_rows = 0U;
  value.baseline_preflight_attempted = false;
  value.baseline_preflight_accepted = false;
  value.baseline_commit_occurred = false;
  return value;
}

ov_msckf::CP2UpdateInvocationContext invocation(
    std::uint64_t pair_index, std::uint64_t timestamp) {
  ov_msckf::CP2UpdateInvocationContext value;
  value.sequence_index = 0U;
  value.pair_index = pair_index;
  value.camera_timestamp_ns = timestamp;
  return value;
}

} // namespace

TEST(CP2SerialRuntimeTrace,
     RecordedRowsJoinContiguousUpdaterIdentitiesAndWriteOnce) {
  TemporaryDirectory root;
  ASSERT_FALSE(root.path.empty());
  ov_msckf::CP2RuntimeContext runtime =
      context(ov_msckf::CP2RuntimeTraceLevel::kRecordedFull, root);
  const std::string serial_path = runtime.serial_trace_path.value;
  ov_msckf::CP2SerialRuntimeTrace trace(std::move(runtime),
                                        {pair(0U, 100U), pair(1U, 200U)});
  ASSERT_TRUE(trace.ready()) << trace.failure();
  ASSERT_TRUE(trace.NoteEnqueue(
      0U, ov_msckf::CP2SerialRuntimeEnqueueStatus::kQueued));
  ASSERT_TRUE(trace.NoteUpdate(update(0U, 0U, 100U)));
  ASSERT_TRUE(trace.NoteProcessing(
      invocation(0U, 100U), true, true, true, false, {{0.0, 0.0, 0.0}},
      {{0.0, 0.0, 0.0, 1.0}}, false));
  ASSERT_TRUE(trace.NoteEnqueue(
      1U, ov_msckf::CP2SerialRuntimeEnqueueStatus::kFrequencyDropped));
  ASSERT_TRUE(trace.Finalize()) << trace.failure();
  EXPECT_FALSE(trace.Finalize());

  const std::string bytes = read_file(serial_path);
  EXPECT_NE(bytes.find("\"record_type\":\"serial_pair\""),
            std::string::npos);
  EXPECT_NE(bytes.find("\"updater_invocation_ids\":[0]"),
            std::string::npos);
  EXPECT_NE(bytes.find("\"enqueue_status\":\"frequency_dropped\""),
            std::string::npos);
  EXPECT_EQ(std::count(bytes.begin(), bytes.end(), '\n'), 2);
}

TEST(CP2SerialRuntimeTrace,
     SequenceRowsRetainNonidentityQuaternionWithoutReordering) {
  TemporaryDirectory root;
  ov_msckf::CP2RuntimeContext runtime =
      context(ov_msckf::CP2RuntimeTraceLevel::kSequence, root);
  const std::string callback_path = runtime.callback_trace_path.value;
  const std::string trajectory_path = runtime.trajectory_trace_path.value;
  ov_msckf::CP2SerialRuntimeTrace trace(std::move(runtime),
                                        {pair(0U, 100U), pair(1U, 200U)});
  ASSERT_TRUE(trace.NoteEnqueue(
      0U, ov_msckf::CP2SerialRuntimeEnqueueStatus::kQueued));
  ASSERT_TRUE(trace.NoteProcessing(
      invocation(0U, 100U), true, true, false, true, {{1.0, 2.0, 3.0}},
      {{0.1, 0.2, 0.3, 0.9}}, false));
  ASSERT_TRUE(trace.NoteEnqueue(
      1U, ov_msckf::CP2SerialRuntimeEnqueueStatus::kQueued));
  ASSERT_TRUE(trace.NoteProcessing(
      invocation(1U, 200U), true, true, false, false, {{0.0, 0.0, 0.0}},
      {{0.0, 0.0, 0.0, 1.0}}, false));
  ASSERT_TRUE(trace.Finalize()) << trace.failure();

  const std::string callbacks = read_file(callback_path);
  const std::string trajectory = read_file(trajectory_path);
  EXPECT_NE(callbacks.find("\"trajectory_index\":0"), std::string::npos);
  EXPECT_NE(callbacks.find("\"trajectory_index\":null"),
            std::string::npos);
  EXPECT_NE(trajectory.find(
                "\"quaternion_ItoG_xyzw\":[0.10000000000000001,"
                "0.20000000000000001,0.29999999999999999,"
                "0.90000000000000002]"),
            std::string::npos);
  EXPECT_NE(trajectory.find("\"position_G\":[1,2,3]"),
            std::string::npos);
}

TEST(CP2SerialRuntimeTrace,
     NoncontiguousWrongPairAndWrongTimestampUpdaterEventsFailSticky) {
  TemporaryDirectory root;
  auto runtime = context(ov_msckf::CP2RuntimeTraceLevel::kRecordedFull, root);
  ov_msckf::CP2SerialRuntimeTrace trace(std::move(runtime), {pair(0U, 100U)});
  ASSERT_TRUE(trace.NoteEnqueue(
      0U, ov_msckf::CP2SerialRuntimeEnqueueStatus::kQueued));
  EXPECT_FALSE(trace.NoteUpdate(update(0U, 1U, 100U)));
  EXPECT_FALSE(trace.ready());
  EXPECT_FALSE(trace.NoteUpdate(update(0U, 0U, 100U)));
  EXPECT_FALSE(trace.Finalize());

  TemporaryDirectory root2;
  auto runtime2 = context(ov_msckf::CP2RuntimeTraceLevel::kRecordedFull,
                          root2);
  ov_msckf::CP2SerialRuntimeTrace wrong_time(std::move(runtime2),
                                             {pair(0U, 100U)});
  ASSERT_TRUE(wrong_time.NoteEnqueue(
      0U, ov_msckf::CP2SerialRuntimeEnqueueStatus::kQueued));
  EXPECT_FALSE(wrong_time.NoteUpdate(update(0U, 0U, 101U)));
}

TEST(CP2SerialRuntimeTrace,
     DuplicateProcessingAndNonfiniteTrajectoryFailBeforeOutput) {
  TemporaryDirectory root;
  auto runtime = context(ov_msckf::CP2RuntimeTraceLevel::kSequence, root);
  const std::string serial_path = runtime.serial_trace_path.value;
  ov_msckf::CP2SerialRuntimeTrace trace(std::move(runtime), {pair(0U, 100U)});
  ASSERT_TRUE(trace.NoteEnqueue(
      0U, ov_msckf::CP2SerialRuntimeEnqueueStatus::kQueued));
  const double nan = std::numeric_limits<double>::quiet_NaN();
  EXPECT_FALSE(trace.NoteProcessing(
      invocation(0U, 100U), true, true, false, true, {{nan, 0.0, 0.0}},
      {{0.0, 0.0, 0.0, 1.0}}, false));
  EXPECT_FALSE(trace.Finalize());
  EXPECT_EQ(::access(serial_path.c_str(), F_OK), -1);
}

TEST(CP2SerialRuntimeTrace, InitialPairPopulationRejectsBoundaryAndGaps) {
  TemporaryDirectory root;
  auto runtime = context(ov_msckf::CP2RuntimeTraceLevel::kRecordedFull, root);
  ov_msckf::CP2SerialPair invalid = pair(1U, 100U);
  invalid.absolute_record_delta_ns =
      ov_msckf::CP2SerialPairSelector::kStrictMaximumRecordDeltaNs;
  ov_msckf::CP2SerialRuntimeTrace trace(std::move(runtime), {invalid});
  EXPECT_FALSE(trace.ready());
}

TEST(CP2SerialRuntimeTrace, OutputCreationRejectsOverwriteAndSymlink) {
  TemporaryDirectory root;
  const std::string existing = root.Add("existing");
  ASSERT_TRUE(ov_msckf::CP2SerialRuntimeTrace::WriteNewFile(existing, "a"));
  struct stat existing_status {};
  ASSERT_EQ(::stat(existing.c_str(), &existing_status), 0);
  EXPECT_TRUE(S_ISREG(existing_status.st_mode));
  EXPECT_EQ(existing_status.st_mode & 0777U, 0444U);
  EXPECT_EQ(existing_status.st_nlink, 1);
  EXPECT_FALSE(ov_msckf::CP2SerialRuntimeTrace::WriteNewFile(existing, "b"));
  EXPECT_EQ(read_file(existing), "a");

  const std::string target = root.Add("target");
  const std::string link = root.Add("link");
  ASSERT_TRUE(ov_msckf::CP2SerialRuntimeTrace::WriteNewFile(target, "x"));
  ASSERT_EQ(::symlink(target.c_str(), link.c_str()), 0);
  EXPECT_FALSE(ov_msckf::CP2SerialRuntimeTrace::WriteNewFile(link, "y"));
  EXPECT_EQ(read_file(target), "x");

  const std::string insecure_parent_output = root.Add("insecure-parent");
  ASSERT_EQ(::chmod(root.path.c_str(), 0755), 0);
  EXPECT_FALSE(ov_msckf::CP2SerialRuntimeTrace::WriteNewFile(
      insecure_parent_output, "z"));
  EXPECT_EQ(::access(insecure_parent_output.c_str(), F_OK), -1);
  ASSERT_EQ(::chmod(root.path.c_str(), 0700), 0);

  HeldDescriptors held;
  const std::string capability_path = root.Add("held-output");
  const ov_msckf::CP2OutputCapability capability =
      create_capability(capability_path, held);
  ov_msckf::CP2OutputCapability parsed;
  ASSERT_TRUE(ov_msckf::ParseCP2OutputCapability(
      encode_capability(capability), parsed));
  EXPECT_EQ(parsed.inode, capability.inode);
  EXPECT_EQ(parsed.ctime_ns, capability.ctime_ns);
  ov_msckf::CP2OutputCapability namespace_init = capability;
  namespace_init.runner_pid = 1U;
  namespace_init.descriptor = 3U;
  ASSERT_TRUE(ov_msckf::ParseCP2OutputCapability(
      encode_capability(namespace_init), parsed));
  std::string namespace_init_path;
  ASSERT_TRUE(ov_msckf::CP2OutputCapabilityPath(
      parsed, namespace_init_path));
  EXPECT_EQ(namespace_init_path, "/proc/1/fd/3");
  EXPECT_FALSE(ov_msckf::ParseCP2OutputCapability(
      "v1:01:3:0:1:33152:1:0:0:0:0:0", parsed));
  ASSERT_TRUE(ov_msckf::ParseCP2OutputCapability("null", parsed));
  EXPECT_FALSE(parsed.available);

  const pid_t child = ::fork();
  ASSERT_GE(child, 0);
  if (child == 0) {
    const bool written = ov_msckf::WriteCP2OutputCapability(
        capability_path, capability, "held-by-parent");
    ::_exit(written ? 0 : 1);
  }
  int child_status = 0;
  ASSERT_EQ(::waitpid(child, &child_status, 0), child);
  ASSERT_TRUE(WIFEXITED(child_status));
  ASSERT_EQ(WEXITSTATUS(child_status), 0);
  EXPECT_EQ(read_held(held.values.front(), 14U), "held-by-parent");
  struct stat sealed_status {};
  ASSERT_EQ(::fstat(held.values.front(), &sealed_status), 0);
  EXPECT_EQ(sealed_status.st_mode & 07777U, 0444U);

  const std::string substituted = root.Add("substituted");
  const std::string displaced = root.Add("substituted-original");
  const ov_msckf::CP2OutputCapability substituted_capability =
      create_capability(substituted, held);
  ASSERT_EQ(::rename(substituted.c_str(), displaced.c_str()), 0);
  const int replacement = ::open(
      substituted.c_str(),
      O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW,
      S_IRUSR | S_IWUSR);
  ASSERT_GE(replacement, 0);
  ASSERT_EQ(::close(replacement), 0);
  EXPECT_FALSE(ov_msckf::WriteCP2OutputCapability(
      substituted, substituted_capability, "forged"));
  ASSERT_EQ(::unlink(substituted.c_str()), 0);
  ASSERT_EQ(::rename(displaced.c_str(), substituted.c_str()), 0);

  // Exercise the exact production bridge used by the VioManager timing
  // writer and both ROS1Visualizer state writers. Every target begins as the
  // required empty 0600 inode, remains held by the parent, and retains its
  // canonical inode/name rather than being unlinked or replaced.
  TemporaryDirectory legacy_root;
  HeldDescriptors legacy_held;
  const std::array<std::string, 3U> legacy_names = {{
      legacy_root.Add("state.txt"),
      legacy_root.Add("deviation.txt"),
      legacy_root.Add("openvins_timing.csv"),
  }};
  std::array<std::string, 3U> legacy_paths;
  std::array<ov_msckf::CP2OutputCapability, 3U> legacy_capabilities;
  std::array<struct stat, 3U> legacy_before{};
  for (std::size_t index = 0U; index < legacy_names.size(); ++index) {
    legacy_capabilities[index] =
        create_capability(legacy_names[index], legacy_held);
    ASSERT_TRUE(ov_msckf::CP2OutputCapabilityPath(
        legacy_capabilities[index], legacy_paths[index]));
    ASSERT_EQ(::stat(legacy_names[index].c_str(), &legacy_before[index]), 0);
  }
  // Both members are still pristine and independently valid here; only the
  // wrong canonical label makes this binding fail.
  std::ofstream rejected_swapped_label;
  EXPECT_FALSE(ov_msckf::OpenCP2PreopenedLegacyStream(
      legacy_names[0], legacy_paths[1], legacy_capabilities[1],
      rejected_swapped_label));
  {
    std::ofstream state_stream;
    std::ofstream deviation_stream;
    std::ofstream timing_stream;
    ASSERT_TRUE(ov_msckf::OpenCP2PreopenedLegacyStream(
        legacy_names[0], legacy_paths[0], legacy_capabilities[0],
        state_stream));
    ASSERT_TRUE(ov_msckf::OpenCP2PreopenedLegacyStream(
        legacy_names[1], legacy_paths[1], legacy_capabilities[1],
        deviation_stream));
    ASSERT_TRUE(ov_msckf::OpenCP2PreopenedLegacyStream(
        legacy_names[2], legacy_paths[2], legacy_capabilities[2],
        timing_stream));
    state_stream << "state-header\n" << std::flush;
    deviation_stream << "deviation-header\n" << std::flush;
    timing_stream << "timing-header\n" << std::flush;
    ASSERT_TRUE(state_stream.good());
    ASSERT_TRUE(deviation_stream.good());
    ASSERT_TRUE(timing_stream.good());
  }
  const std::array<std::string, 3U> legacy_expected = {{
      "state-header\n",
      "deviation-header\n",
      "timing-header\n",
  }};
  for (std::size_t index = 0U; index < legacy_names.size(); ++index) {
    struct stat after {};
    ASSERT_EQ(::stat(legacy_names[index].c_str(), &after), 0);
    EXPECT_EQ(after.st_dev, legacy_before[index].st_dev);
    EXPECT_EQ(after.st_ino, legacy_before[index].st_ino);
    EXPECT_EQ(after.st_nlink, 1);
    EXPECT_EQ(read_file(legacy_names[index]), legacy_expected[index]);
  }

  // Any intervening write after capability creation is evidence drift, not
  // content to append to. Reject it without truncating or replacing the inode.
  const std::string nonempty_name = legacy_root.Add("nonempty.txt");
  const ov_msckf::CP2OutputCapability nonempty_capability =
      create_capability(nonempty_name, legacy_held);
  std::string nonempty_path;
  ASSERT_TRUE(ov_msckf::CP2OutputCapabilityPath(
      nonempty_capability, nonempty_path));
  const int unexpected_writer =
      ::open(nonempty_path.c_str(), O_WRONLY | O_CLOEXEC);
  ASSERT_GE(unexpected_writer, 0);
  ASSERT_EQ(::write(unexpected_writer, "x", 1U), 1);
  ASSERT_EQ(::close(unexpected_writer), 0);
  std::ofstream rejected_nonempty;
  EXPECT_FALSE(ov_msckf::OpenCP2PreopenedLegacyStream(
      nonempty_name, nonempty_path, nonempty_capability,
      rejected_nonempty));
  EXPECT_EQ(read_file(nonempty_name), "x");

  TemporaryDirectory formal_root;
  HeldDescriptors formal_held;
  ov_msckf::CP2RuntimeContext formal_context =
      context(ov_msckf::CP2RuntimeTraceLevel::kSequence, formal_root);
  ov_msckf::CP2RuntimeOutputCapabilities formal_capabilities;
  formal_capabilities.serial_trace = create_capability(
      formal_context.serial_trace_path.value, formal_held);
  formal_capabilities.callback_trace = create_capability(
      formal_context.callback_trace_path.value, formal_held);
  formal_capabilities.trajectory_trace = create_capability(
      formal_context.trajectory_trace_path.value, formal_held);
  formal_capabilities.runtime_parameters = create_capability(
      formal_context.runtime_parameters_path.value, formal_held);
  formal_capabilities.loader_map_before = create_capability(
      formal_context.loader_map_before_path.value, formal_held);
  formal_capabilities.loader_map_after = create_capability(
      formal_context.loader_map_after_path.value, formal_held);
  formal_capabilities.legacy_state = create_capability(
      formal_context.legacy_state_path.value, formal_held);
  formal_capabilities.legacy_deviation = create_capability(
      formal_context.legacy_deviation_path.value, formal_held);
  formal_capabilities.legacy_timing = create_capability(
      formal_context.legacy_timing_path.value, formal_held);
  ASSERT_TRUE(ov_msckf::ValidateCP2RuntimeOutputCapabilities(
      formal_context, formal_capabilities));
  ov_msckf::CP2RuntimeOutputCapabilities duplicate = formal_capabilities;
  duplicate.callback_trace = duplicate.serial_trace;
  EXPECT_FALSE(ov_msckf::ValidateCP2RuntimeOutputCapabilities(
      formal_context, duplicate));
  ov_msckf::CP2SerialRuntimeTrace formal_trace(
      std::move(formal_context), formal_capabilities, {pair(0U, 100U)});
  ASSERT_TRUE(formal_trace.ready()) << formal_trace.failure();
  ASSERT_TRUE(formal_trace.NoteEnqueue(
      0U, ov_msckf::CP2SerialRuntimeEnqueueStatus::kQueued));
  ASSERT_TRUE(formal_trace.NoteProcessing(
      invocation(0U, 100U), true, true, false, false,
      {{0.0, 0.0, 0.0}}, {{0.0, 0.0, 0.0, 1.0}}, false));
  EXPECT_TRUE(formal_trace.Finalize()) << formal_trace.failure();
}

TEST(CP2OutputCapability, LevelPopulationsAreClosedAndExact) {
  const std::array<ov_msckf::CP2RuntimeTraceLevel, 3U> levels = {{
      ov_msckf::CP2RuntimeTraceLevel::kRecordedFull,
      ov_msckf::CP2RuntimeTraceLevel::kSequence,
      ov_msckf::CP2RuntimeTraceLevel::kTiming,
  }};
  for (const ov_msckf::CP2RuntimeTraceLevel level : levels) {
    TemporaryDirectory root;
    HeldDescriptors held;
    const ov_msckf::CP2RuntimeContext runtime = context(level, root);
    const ov_msckf::CP2RuntimeOutputCapabilities population =
        capability_population(runtime, held);
    ASSERT_TRUE(ov_msckf::ValidateCP2RuntimeOutputCapabilities(
        runtime, population));

    ov_msckf::CP2RuntimeOutputCapabilities missing = population;
    missing.serial_trace = ov_msckf::CP2OutputCapability{};
    EXPECT_FALSE(ov_msckf::ValidateCP2RuntimeOutputCapabilities(
        runtime, missing));

    ov_msckf::CP2RuntimeOutputCapabilities extra = population;
    extra.state_payload =
        create_capability(root.Add("forbidden-extra.bin"), held);
    EXPECT_FALSE(ov_msckf::ValidateCP2RuntimeOutputCapabilities(
        runtime, extra));
  }
}

TEST(CP2OutputCapability,
     SealRejectsWrongSizeWrongOffsetAndConcurrentGrowth) {
  {
    TemporaryDirectory root;
    HeldDescriptors held;
    const std::string name = root.Add("wrong-size.bin");
    const ov_msckf::CP2OutputCapability capability =
        create_capability(name, held);
    ov_msckf::CP2OpenedOutput output;
    ASSERT_TRUE(ov_msckf::OpenCP2OutputCapability(
        name, capability, output));
    ASSERT_EQ(::write(output.descriptor(), "abc", 3U), 3);
    EXPECT_FALSE(ov_msckf::SealCP2OutputCapability(output, 2U));
    struct stat status {};
    ASSERT_EQ(::stat(name.c_str(), &status), 0);
    EXPECT_EQ(status.st_size, 3);
    EXPECT_EQ(status.st_mode & 07777U, 0600U);
  }

  {
    TemporaryDirectory root;
    HeldDescriptors held;
    const std::string name = root.Add("wrong-offset.bin");
    const ov_msckf::CP2OutputCapability capability =
        create_capability(name, held);
    ov_msckf::CP2OpenedOutput output;
    ASSERT_TRUE(ov_msckf::OpenCP2OutputCapability(
        name, capability, output));
    ASSERT_EQ(::write(output.descriptor(), "abc", 3U), 3);
    ASSERT_EQ(::lseek(output.descriptor(), 0, SEEK_SET), 0);
    EXPECT_FALSE(ov_msckf::SealCP2OutputCapability(output, 3U));
  }

  {
    TemporaryDirectory root;
    HeldDescriptors held;
    const std::string name = root.Add("concurrent-growth.bin");
    const ov_msckf::CP2OutputCapability capability =
        create_capability(name, held);
    ov_msckf::CP2OpenedOutput output;
    ASSERT_TRUE(ov_msckf::OpenCP2OutputCapability(
        name, capability, output));
    ASSERT_EQ(::write(output.descriptor(), "abc", 3U), 3);
    std::string capability_path;
    ASSERT_TRUE(ov_msckf::CP2OutputCapabilityPath(
        capability, capability_path));
    const int competing =
        ::open(capability_path.c_str(), O_WRONLY | O_CLOEXEC);
    ASSERT_GE(competing, 0);
    ASSERT_EQ(::pwrite(competing, "x", 1U, 3), 1);
    ASSERT_EQ(::close(competing), 0);
    EXPECT_FALSE(ov_msckf::SealCP2OutputCapability(output, 3U));
    EXPECT_EQ(read_file(name), "abcx");
  }
}

TEST(CP2TimingClock, TickConversionIsExactAndRejectsNegativeEpoch) {
  std::uint64_t converted = 0U;
  EXPECT_TRUE(ov_msckf::cp2_steady_clock_from_ticks(0, converted));
  EXPECT_EQ(converted, 0U);
  EXPECT_TRUE(ov_msckf::cp2_steady_clock_from_ticks(
      std::numeric_limits<std::int64_t>::max(), converted));
  EXPECT_EQ(converted, static_cast<std::uint64_t>(
                           std::numeric_limits<std::int64_t>::max()));
  converted = 7U;
  EXPECT_FALSE(ov_msckf::cp2_steady_clock_from_ticks(-1, converted));
  EXPECT_EQ(converted, 0U);
}

TEST(CP2TimingClock, DurationRejectsInvalidAndReverseOrderedEndpoints) {
  const ov_msckf::CP2SteadyClockEndpoint start{10U, true};
  const ov_msckf::CP2SteadyClockEndpoint same{10U, true};
  const ov_msckf::CP2SteadyClockEndpoint end{25U, true};
  const ov_msckf::CP2SteadyClockEndpoint reverse{9U, true};
  const ov_msckf::CP2SteadyClockEndpoint invalid{25U, false};
  const ov_msckf::CP2SteadyClockEndpoint zero{0U, true};
  const ov_msckf::CP2SteadyClockEndpoint maximum{
      std::numeric_limits<std::uint64_t>::max(), true};
  std::uint64_t duration = 99U;
  EXPECT_TRUE(ov_msckf::cp2_steady_clock_duration(start, same, duration));
  EXPECT_EQ(duration, 0U);
  EXPECT_TRUE(ov_msckf::cp2_steady_clock_duration(start, end, duration));
  EXPECT_EQ(duration, 15U);
  EXPECT_TRUE(ov_msckf::cp2_steady_clock_duration(zero, maximum, duration));
  EXPECT_EQ(duration, std::numeric_limits<std::uint64_t>::max());
  EXPECT_FALSE(
      ov_msckf::cp2_steady_clock_duration(start, reverse, duration));
  EXPECT_EQ(duration, 0U);
  EXPECT_FALSE(
      ov_msckf::cp2_steady_clock_duration(start, invalid, duration));
  EXPECT_EQ(duration, 0U);
}

TEST(CP2TimingClock, LiveSteadyClockSamplesAreValidAndOrdered) {
  const ov_msckf::CP2SteadyClockEndpoint first =
      ov_msckf::cp2_steady_clock_now();
  const ov_msckf::CP2SteadyClockEndpoint second =
      ov_msckf::cp2_steady_clock_now();
  ASSERT_TRUE(first.valid);
  ASSERT_TRUE(second.valid);
  std::uint64_t duration = 0U;
  EXPECT_TRUE(
      ov_msckf::cp2_steady_clock_duration(first, second, duration));
}

TEST(CP2SerialRuntimeTrace,
     TimingWritesCompleteUpdaterPopulationAndExactSteadyEndpoints) {
  TemporaryDirectory root;
  auto runtime = context(ov_msckf::CP2RuntimeTraceLevel::kTiming, root);
  const std::string serial_path = runtime.serial_trace_path.value;
  const std::string callback_path = runtime.callback_trace_path.value;
  const std::string updater_path = runtime.updater_trace_path.value;
  const std::string timing_path = runtime.timing_trace_path.value;
  ov_msckf::CP2SerialRuntimeTrace trace(
      std::move(runtime), {pair(0U, 100U), pair(1U, 200U)});

  ASSERT_TRUE(trace.NoteEnqueue(
      0U, ov_msckf::CP2SerialRuntimeEnqueueStatus::kQueued));
  ASSERT_TRUE(trace.NoteUpdate(update(0U, 0U, 100U)));
  ASSERT_TRUE(trace.NoteProcessing(
      invocation(0U, 100U), true, true, true, false,
      {{0.0, 0.0, 0.0}}, {{0.0, 0.0, 0.0, 1.0}}, false));
  ASSERT_TRUE(trace.NoteEnqueue(
      1U, ov_msckf::CP2SerialRuntimeEnqueueStatus::kQueued));
  ASSERT_TRUE(trace.NoteUpdate(empty_update(1U, 1U, 200U)));
  ASSERT_TRUE(trace.NoteProcessing(
      invocation(1U, 200U), true, true, true, false,
      {{0.0, 0.0, 0.0}}, {{0.0, 0.0, 0.0, 1.0}}, false));
  ASSERT_TRUE(trace.Finalize()) << trace.failure();

  const std::string serial = read_file(serial_path);
  const std::string callbacks = read_file(callback_path);
  const std::string updater = read_file(updater_path);
  const std::string timing = read_file(timing_path);
  EXPECT_EQ(std::count(serial.begin(), serial.end(), '\n'), 2);
  EXPECT_EQ(std::count(callbacks.begin(), callbacks.end(), '\n'), 2);
  EXPECT_EQ(std::count(updater.begin(), updater.end(), '\n'), 2);
  EXPECT_EQ(std::count(timing.begin(), timing.end(), '\n'), 2);
  EXPECT_NE(callbacks.find("\"updater_invocation_ids\":[0]"),
            std::string::npos);
  EXPECT_NE(callbacks.find("\"updater_invoked\":true"),
            std::string::npos);
  EXPECT_NE(updater.find("\"primary\":true"), std::string::npos);
  EXPECT_NE(updater.find("\"primary\":false"), std::string::npos);
  EXPECT_NE(timing.find(
                "\"timer_clock\":\"std::chrono::steady_clock\""),
            std::string::npos);
  EXPECT_NE(timing.find("\"timer_start_ns\":1000"),
            std::string::npos);
  EXPECT_NE(timing.find("\"timer_end_ns\":1025"), std::string::npos);
  EXPECT_NE(timing.find("\"duration_ns\":25"), std::string::npos);
  EXPECT_NE(timing.find("\"terminal_status\":\"empty_input\""),
            std::string::npos);
}

TEST(CP2SerialRuntimeTrace,
     TimingRejectsReverseMismatchedInvalidDuplicateAndIncompleteRows) {
  {
    TemporaryDirectory root;
    auto runtime = context(ov_msckf::CP2RuntimeTraceLevel::kTiming, root);
    ov_msckf::CP2SerialRuntimeTrace trace(std::move(runtime),
                                          {pair(0U, 100U)});
    ASSERT_TRUE(trace.NoteEnqueue(
        0U, ov_msckf::CP2SerialRuntimeEnqueueStatus::kQueued));
    ov_msckf::CP2LiveUpdateEvent event = update(0U, 0U, 100U);
    event.timing_end_ns = event.timing_start_ns - 1U;
    EXPECT_FALSE(trace.NoteUpdate(event));
  }
  {
    TemporaryDirectory root;
    auto runtime = context(ov_msckf::CP2RuntimeTraceLevel::kTiming, root);
    ov_msckf::CP2SerialRuntimeTrace trace(std::move(runtime),
                                          {pair(0U, 100U)});
    ASSERT_TRUE(trace.NoteEnqueue(
        0U, ov_msckf::CP2SerialRuntimeEnqueueStatus::kQueued));
    ov_msckf::CP2LiveUpdateEvent event = update(0U, 0U, 100U);
    ++event.duration_ns;
    EXPECT_FALSE(trace.NoteUpdate(event));
  }
  {
    TemporaryDirectory root;
    auto runtime = context(ov_msckf::CP2RuntimeTraceLevel::kTiming, root);
    ov_msckf::CP2SerialRuntimeTrace trace(std::move(runtime),
                                          {pair(0U, 100U)});
    ASSERT_TRUE(trace.NoteEnqueue(
        0U, ov_msckf::CP2SerialRuntimeEnqueueStatus::kQueued));
    ov_msckf::CP2LiveUpdateEvent event = update(0U, 0U, 100U);
    event.timing_endpoint_valid = false;
    EXPECT_FALSE(trace.NoteUpdate(event));
  }
  {
    TemporaryDirectory root;
    auto runtime = context(ov_msckf::CP2RuntimeTraceLevel::kTiming, root);
    ov_msckf::CP2SerialRuntimeTrace trace(std::move(runtime),
                                          {pair(0U, 100U)});
    ASSERT_TRUE(trace.NoteEnqueue(
        0U, ov_msckf::CP2SerialRuntimeEnqueueStatus::kQueued));
    ASSERT_TRUE(trace.NoteUpdate(update(0U, 0U, 100U)));
    EXPECT_FALSE(trace.NoteUpdate(update(0U, 1U, 100U)));
  }
  {
    TemporaryDirectory root;
    auto runtime = context(ov_msckf::CP2RuntimeTraceLevel::kTiming, root);
    const std::string serial_path = runtime.serial_trace_path.value;
    ov_msckf::CP2SerialRuntimeTrace trace(std::move(runtime),
                                          {pair(0U, 100U)});
    ASSERT_TRUE(trace.NoteEnqueue(
        0U, ov_msckf::CP2SerialRuntimeEnqueueStatus::kQueued));
    ASSERT_TRUE(trace.NoteUpdate(update(0U, 0U, 100U)));
    EXPECT_FALSE(trace.Finalize());
    EXPECT_EQ(::access(serial_path.c_str(), F_OK), -1);
  }
  {
    TemporaryDirectory root;
    auto runtime = context(ov_msckf::CP2RuntimeTraceLevel::kTiming, root);
    ov_msckf::CP2SerialRuntimeTrace trace(std::move(runtime),
                                          {pair(0U, 100U)});
    ASSERT_TRUE(trace.NoteEnqueue(
        0U, ov_msckf::CP2SerialRuntimeEnqueueStatus::kQueued));
    ov_msckf::CP2LiveUpdateEvent event = update(0U, 0U, 100U);
    event.baseline_preflight_accepted = false;
    EXPECT_FALSE(trace.NoteUpdate(event));
  }
  {
    TemporaryDirectory root;
    auto runtime = context(ov_msckf::CP2RuntimeTraceLevel::kTiming, root);
    ov_msckf::CP2SerialRuntimeTrace trace(std::move(runtime),
                                          {pair(0U, 100U)});
    ASSERT_TRUE(trace.NoteEnqueue(
        0U, ov_msckf::CP2SerialRuntimeEnqueueStatus::kQueued));
    ov_msckf::CP2LiveUpdateEvent event = update(0U, 0U, 100U);
    event.terminal_status =
        ov_msckf::CP2UpdateTerminalStatus::kAllRejected;
    event.terminal_subreason =
        ov_msckf::CP2UpdateTerminalSubreason::kNoRawSystems;
    event.baseline_commit_occurred = false;
    EXPECT_FALSE(trace.NoteUpdate(event));
  }
  {
    TemporaryDirectory root;
    auto runtime = context(ov_msckf::CP2RuntimeTraceLevel::kTiming, root);
    ov_msckf::CP2SerialRuntimeTrace trace(std::move(runtime),
                                          {pair(0U, 100U)});
    ASSERT_TRUE(trace.NoteEnqueue(
        0U, ov_msckf::CP2SerialRuntimeEnqueueStatus::kQueued));
    ov_msckf::CP2LiveUpdateEvent event = update(0U, 0U, 100U);
    event.terminal_status =
        ov_msckf::CP2UpdateTerminalStatus::kAllRejected;
    event.terminal_subreason =
        ov_msckf::CP2UpdateTerminalSubreason::kAllBaselineFeaturesRejected;
    event.baseline_precompression_rows = 0U;
    event.baseline_precompression_system_nonempty = false;
    event.baseline_compressed_rows_available = false;
    event.baseline_compressed_rows = 0U;
    event.baseline_compressed_system_nonempty = false;
    event.baseline_preflight_attempted = false;
    event.baseline_preflight_accepted = false;
    event.baseline_commit_occurred = false;
    EXPECT_TRUE(trace.NoteUpdate(event));
  }
  {
    TemporaryDirectory root;
    auto runtime = context(ov_msckf::CP2RuntimeTraceLevel::kTiming, root);
    ov_msckf::CP2SerialRuntimeTrace trace(std::move(runtime),
                                          {pair(0U, 100U)});
    ASSERT_TRUE(trace.NoteEnqueue(
        0U, ov_msckf::CP2SerialRuntimeEnqueueStatus::kQueued));
    ov_msckf::CP2LiveUpdateEvent event =
        empty_update(0U, 0U, 100U);
    event.input_feature_count = 1U;
    event.terminal_status =
        ov_msckf::CP2UpdateTerminalStatus::kAllRejected;
    event.terminal_subreason =
        ov_msckf::CP2UpdateTerminalSubreason::kNoRawSystems;
    EXPECT_TRUE(trace.NoteUpdate(event));
  }
  {
    TemporaryDirectory root;
    auto runtime = context(ov_msckf::CP2RuntimeTraceLevel::kTiming, root);
    ov_msckf::CP2SerialRuntimeTrace trace(std::move(runtime),
                                          {pair(0U, 100U)});
    ASSERT_TRUE(trace.NoteEnqueue(
        0U, ov_msckf::CP2SerialRuntimeEnqueueStatus::kQueued));
    ov_msckf::CP2LiveUpdateEvent event = update(0U, 0U, 100U);
    event.baseline_preflight_attempted = false;
    EXPECT_FALSE(trace.NoteUpdate(event));
  }
  {
    TemporaryDirectory root;
    auto runtime = context(ov_msckf::CP2RuntimeTraceLevel::kTiming, root);
    ov_msckf::CP2SerialRuntimeTrace trace(std::move(runtime),
                                          {pair(0U, 100U)});
    ASSERT_TRUE(trace.NoteEnqueue(
        0U, ov_msckf::CP2SerialRuntimeEnqueueStatus::kQueued));
    ov_msckf::CP2LiveUpdateEvent event = update(0U, 0U, 100U);
    event.baseline_compressed_rows =
        event.baseline_precompression_rows + 1U;
    EXPECT_FALSE(trace.NoteUpdate(event));
  }
  {
    TemporaryDirectory root;
    auto runtime = context(ov_msckf::CP2RuntimeTraceLevel::kTiming, root);
    ov_msckf::CP2SerialRuntimeTrace trace(std::move(runtime),
                                          {pair(0U, 100U)});
    ASSERT_TRUE(trace.NoteEnqueue(
        0U, ov_msckf::CP2SerialRuntimeEnqueueStatus::kQueued));
    ov_msckf::CP2LiveUpdateEvent event = update(0U, 0U, 100U);
    event.terminal_status =
        ov_msckf::CP2UpdateTerminalStatus::kPreflightRejected;
    event.terminal_subreason =
        ov_msckf::CP2UpdateTerminalSubreason::kBaselinePreflightRejected;
    event.baseline_preflight_accepted = false;
    event.baseline_commit_occurred = false;
    EXPECT_TRUE(trace.NoteUpdate(event));
  }
}
