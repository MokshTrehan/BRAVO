/*
 * SchurVIO-Lite CP2 serial runtime trace tests.
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include "update/CP2SerialRuntimeTrace.h"

#include <gtest/gtest.h>

#include <algorithm>
#include <array>
#include <cstdio>
#include <fstream>
#include <iterator>
#include <limits>
#include <string>
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
  if (level == ov_msckf::CP2RuntimeTraceLevel::kSequence) {
    value.callback_trace_path.available = true;
    value.callback_trace_path.value = root.Add("callbacks.jsonl");
    value.trajectory_trace_path.available = true;
    value.trajectory_trace_path.value = root.Add("trajectory.jsonl");
  }
  return value;
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
  EXPECT_FALSE(ov_msckf::CP2SerialRuntimeTrace::WriteNewFile(existing, "b"));
  EXPECT_EQ(read_file(existing), "a");

  const std::string target = root.Add("target");
  const std::string link = root.Add("link");
  ASSERT_TRUE(ov_msckf::CP2SerialRuntimeTrace::WriteNewFile(target, "x"));
  ASSERT_EQ(::symlink(target.c_str(), link.c_str()), 0);
  EXPECT_FALSE(ov_msckf::CP2SerialRuntimeTrace::WriteNewFile(link, "y"));
  EXPECT_EQ(read_file(target), "x");
}
