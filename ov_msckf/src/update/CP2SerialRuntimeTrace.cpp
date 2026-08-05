/*
 * SchurVIO-Lite CP2 value-only serial runtime trace.
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include "CP2SerialRuntimeTrace.h"

#include <algorithm>
#include <array>
#include <cerrno>
#include <cmath>
#include <fcntl.h>
#include <iomanip>
#include <limits>
#include <locale>
#include <set>
#include <sstream>
#include <sys/stat.h>
#include <unistd.h>
#include <utility>

namespace {

class ScopedDescriptor {
public:
  explicit ScopedDescriptor(int value = -1) noexcept : value_(value) {}
  ~ScopedDescriptor() {
    if (value_ >= 0) {
      (void)::close(value_);
    }
  }
  ScopedDescriptor(const ScopedDescriptor &) = delete;
  ScopedDescriptor &operator=(const ScopedDescriptor &) = delete;
  int get() const noexcept { return value_; }

private:
  int value_;
};

bool write_all(int descriptor, const std::uint8_t *data,
               std::size_t size) noexcept {
  std::size_t offset = 0U;
  while (offset < size) {
    const ssize_t count = ::write(descriptor, data + offset, size - offset);
    if (count < 0 && errno == EINTR) {
      continue;
    }
    if (count <= 0) {
      return false;
    }
    offset += static_cast<std::size_t>(count);
  }
  return true;
}

std::string json_string(const std::string &value) {
  std::ostringstream output;
  output.imbue(std::locale::classic());
  output << '"';
  static constexpr char kHex[] = "0123456789abcdef";
  for (unsigned char byte : value) {
    switch (byte) {
    case '"': output << "\\\""; break;
    case '\\': output << "\\\\"; break;
    case '\b': output << "\\b"; break;
    case '\f': output << "\\f"; break;
    case '\n': output << "\\n"; break;
    case '\r': output << "\\r"; break;
    case '\t': output << "\\t"; break;
    default:
      if (byte < 0x20U) {
        output << "\\u00" << kHex[(byte >> 4U) & 0x0fU]
               << kHex[byte & 0x0fU];
      } else {
        output << static_cast<char>(byte);
      }
    }
  }
  output << '"';
  return output.str();
}

const char *json_bool(bool value) noexcept { return value ? "true" : "false"; }

std::string json_u64_array(const std::vector<std::uint64_t> &values) {
  std::ostringstream output;
  output.imbue(std::locale::classic());
  output << '[';
  for (std::size_t index = 0U; index < values.size(); ++index) {
    if (index != 0U) {
      output << ',';
    }
    output << values[index];
  }
  output << ']';
  return output.str();
}

bool finite_pose(const std::array<double, 3U> &position,
                 const std::array<double, 4U> &quaternion) noexcept {
  return std::all_of(position.begin(), position.end(),
                     [](double value) { return std::isfinite(value); }) &&
         std::all_of(quaternion.begin(), quaternion.end(),
                     [](double value) { return std::isfinite(value); });
}

bool terminal_pair_valid(const ov_msckf::CP2LiveUpdateEvent &event) noexcept {
  using Status = ov_msckf::CP2UpdateTerminalStatus;
  using Subreason = ov_msckf::CP2UpdateTerminalSubreason;
  switch (event.terminal_status) {
  case Status::kEmptyInput:
    return event.terminal_subreason == Subreason::kInputEmpty;
  case Status::kAllRejected:
    if (event.raw_system_count == 0U) {
      return event.terminal_subreason ==
                 Subreason::kNoFeaturesAfterCleaning ||
             event.terminal_subreason ==
                 Subreason::kNoFeaturesAfterTriangulation ||
             event.terminal_subreason == Subreason::kNoRawSystems;
    }
    return event.terminal_subreason ==
           Subreason::kAllBaselineFeaturesRejected;
  case Status::kEmptyAfterCompression:
    return event.terminal_subreason ==
           Subreason::kMeasurementCompressionEmpty;
  case Status::kPreflightRejected:
    return event.terminal_subreason ==
           Subreason::kBaselinePreflightRejected;
  case Status::kCommittedCounted:
    return event.terminal_subreason == Subreason::kNone;
  case Status::kInternalFailure:
    return false;
  }
  return false;
}

bool timing_lifecycle_valid(
    const ov_msckf::CP2LiveUpdateEvent &event) noexcept {
  std::uint64_t reconstructed_duration = 0U;
  const bool nonempty = event.input_feature_count != 0U;
  const bool committed =
      event.terminal_status ==
      ov_msckf::CP2UpdateTerminalStatus::kCommittedCounted;
  if (!event.timing_endpoint_valid ||
      event.timing_end_ns < event.timing_start_ns) {
    return false;
  }
  reconstructed_duration =
      event.timing_end_ns - event.timing_start_ns;
  const bool expected_precompression_rows_available =
      event.raw_system_count != 0U;
  const bool expected_precompression_nonempty =
      event.baseline_precompression_rows_available &&
      event.baseline_precompression_rows != 0U;
  const bool expected_compressed_rows_available =
      event.baseline_precompression_system_nonempty;
  const bool expected_compressed_nonempty =
      event.baseline_compressed_rows_available &&
      event.baseline_compressed_rows != 0U;
  const bool expected_preflight_attempted =
      event.baseline_compressed_system_nonempty;
  ov_msckf::CP2UpdateTerminalStatus reconstructed_status =
      ov_msckf::CP2UpdateTerminalStatus::kInternalFailure;
  if (!nonempty) {
    reconstructed_status = ov_msckf::CP2UpdateTerminalStatus::kEmptyInput;
  } else if (!event.baseline_precompression_system_nonempty) {
    reconstructed_status = ov_msckf::CP2UpdateTerminalStatus::kAllRejected;
  } else if (!event.baseline_compressed_system_nonempty) {
    reconstructed_status =
        ov_msckf::CP2UpdateTerminalStatus::kEmptyAfterCompression;
  } else if (!event.baseline_preflight_accepted) {
    reconstructed_status =
        ov_msckf::CP2UpdateTerminalStatus::kPreflightRejected;
  } else {
    reconstructed_status =
        ov_msckf::CP2UpdateTerminalStatus::kCommittedCounted;
  }
  if (event.duration_ns != reconstructed_duration ||
      !terminal_pair_valid(event) ||
      event.terminal_status != reconstructed_status ||
      event.raw_system_count > event.input_feature_count ||
      event.baseline_precompression_rows_available !=
          expected_precompression_rows_available ||
      (!event.baseline_precompression_rows_available &&
       event.baseline_precompression_rows != 0U) ||
      event.baseline_precompression_system_nonempty !=
          expected_precompression_nonempty ||
      event.baseline_compressed_rows_available !=
          expected_compressed_rows_available ||
      (!event.baseline_compressed_rows_available &&
       event.baseline_compressed_rows != 0U) ||
      event.baseline_compressed_system_nonempty !=
          expected_compressed_nonempty ||
      (event.baseline_compressed_rows_available &&
       event.baseline_compressed_rows >
           event.baseline_precompression_rows) ||
      event.baseline_preflight_attempted !=
          expected_preflight_attempted ||
      event.baseline_preflight_accepted != committed ||
      event.baseline_commit_occurred != committed) {
    return false;
  }
  return true;
}

} // namespace

const char *ov_msckf::cp2_serial_runtime_enqueue_status_name(
    CP2SerialRuntimeEnqueueStatus status) noexcept {
  switch (status) {
  case CP2SerialRuntimeEnqueueStatus::kQueued: return "queued";
  case CP2SerialRuntimeEnqueueStatus::kFrequencyDropped:
    return "frequency_dropped";
  case CP2SerialRuntimeEnqueueStatus::kCam0DecodeFailed:
    return "cam0_decode_failed";
  case CP2SerialRuntimeEnqueueStatus::kCam1DecodeFailed:
    return "cam1_decode_failed";
  case CP2SerialRuntimeEnqueueStatus::kProcessTerminated:
    return "process_terminated";
  case CP2SerialRuntimeEnqueueStatus::kTraceFailure: return "trace_failure";
  }
  return "invalid";
}

ov_msckf::CP2SerialRuntimeTrace::CP2SerialRuntimeTrace(
    CP2RuntimeContext context, std::vector<CP2SerialPair> pairs) noexcept
    : context_(std::move(context)) {
  try {
    rows_.reserve(pairs.size());
    for (CP2SerialPair &pair : pairs) {
      Row row;
      row.pair = std::move(pair);
      rows_.push_back(std::move(row));
    }
    (void)ValidateInitialPopulation();
  } catch (...) {
    Reject("serial trace population allocation failed");
  }
}

ov_msckf::CP2SerialRuntimeTrace::CP2SerialRuntimeTrace(
    CP2RuntimeContext context,
    CP2RuntimeOutputCapabilities output_capabilities,
    std::vector<CP2SerialPair> pairs) noexcept
    : context_(std::move(context)),
      output_capabilities_(std::move(output_capabilities)),
      held_capability_mode_(true) {
  try {
    if (!ValidateCP2RuntimeOutputCapabilities(context_,
                                               output_capabilities_)) {
      Reject("serial trace output capability population is invalid");
      return;
    }
    rows_.reserve(pairs.size());
    for (CP2SerialPair &selected : pairs) {
      Row row;
      row.pair = std::move(selected);
      rows_.push_back(std::move(row));
    }
    (void)ValidateInitialPopulation();
  } catch (...) {
    Reject("serial trace population allocation failed");
  }
}

void ov_msckf::CP2SerialRuntimeTrace::Reject(const char *message) noexcept {
  if (!failed_) {
    failed_ = true;
    try {
      failure_ = message;
    } catch (...) {
      // failed_ is the allocation-independent sticky state.
    }
  }
}

bool ov_msckf::CP2SerialRuntimeTrace::ValidateInitialPopulation() noexcept {
  try {
    std::set<std::uint64_t> used_camera_indices;
    std::uint64_t previous_anchor = 0U;
    bool have_previous_anchor = false;
    for (std::size_t index = 0U; index < rows_.size(); ++index) {
      const CP2SerialPair &pair = rows_[index].pair;
      const std::uint64_t recomputed_delta =
          pair.cam0_record_time_ns >= pair.cam1_record_time_ns
              ? pair.cam0_record_time_ns - pair.cam1_record_time_ns
              : pair.cam1_record_time_ns - pair.cam0_record_time_ns;
      if (index > static_cast<std::size_t>(
                      std::numeric_limits<std::uint64_t>::max()) ||
          pair.sequence_index != context_.sequence_index ||
          pair.pair_index != static_cast<std::uint64_t>(index) ||
          pair.anchor_camera_id > 1U ||
          (pair.anchor_camera_id == 0U &&
           pair.anchor_filtered_index != pair.cam0_filtered_index) ||
          (pair.anchor_camera_id == 1U &&
           pair.anchor_filtered_index != pair.cam1_filtered_index) ||
          pair.cam0_filtered_index == pair.cam1_filtered_index ||
          (have_previous_anchor &&
           pair.anchor_filtered_index <= previous_anchor) ||
          !used_camera_indices.insert(pair.cam0_filtered_index).second ||
          !used_camera_indices.insert(pair.cam1_filtered_index).second ||
          pair.camera_timestamp_ns != pair.cam0_header_time_ns ||
          pair.absolute_record_delta_ns != recomputed_delta ||
          pair.absolute_record_delta_ns >=
              CP2SerialPairSelector::kStrictMaximumRecordDeltaNs) {
        Reject("serial trace initial pair population is invalid");
        return false;
      }
      previous_anchor = pair.anchor_filtered_index;
      have_previous_anchor = true;
    }
  } catch (...) {
    Reject("serial trace initial population validation failed");
    return false;
  }
  return ready();
}

bool ov_msckf::CP2SerialRuntimeTrace::NoteEnqueue(
    std::uint64_t pair_index,
    CP2SerialRuntimeEnqueueStatus status) noexcept {
  if (!ready() || finalized_ || pair_index >= rows_.size()) {
    Reject("serial enqueue identity is invalid");
    return false;
  }
  Row &row = rows_[static_cast<std::size_t>(pair_index)];
  if (row.enqueue_noted) {
    Reject("serial pair was enqueued more than once");
    return false;
  }
  row.enqueue_noted = true;
  row.enqueue_entered = true;
  row.enqueue_returned = true;
  row.enqueue_status = cp2_serial_runtime_enqueue_status_name(status);
  if (status == CP2SerialRuntimeEnqueueStatus::kQueued) {
    row.processing_status = "queued_unprocessed";
  }
  return true;
}

bool ov_msckf::CP2SerialRuntimeTrace::NoteProcessing(
    const CP2UpdateInvocationContext &invocation_context,
    bool processing_entered,
    bool processing_returned, bool updater_invoked, bool state_row_emitted,
    const std::array<double, 3U> &position_G,
    const std::array<double, 4U> &quaternion_ItoG_xyzw,
    bool trace_fatal) noexcept {
  if (!ready() || finalized_ ||
      invocation_context.sequence_index != context_.sequence_index ||
      invocation_context.pair_index >= rows_.size()) {
    Reject("serial processing identity is invalid");
    return false;
  }
  Row &row =
      rows_[static_cast<std::size_t>(invocation_context.pair_index)];
  if (!row.enqueue_noted || row.enqueue_status != "queued" ||
      invocation_context.camera_timestamp_ns != row.pair.camera_timestamp_ns ||
      row.processing_noted || !processing_entered ||
      (state_row_emitted && !processing_returned) ||
      (state_row_emitted && !finite_pose(position_G, quaternion_ItoG_xyzw))) {
    Reject("serial processing event is inconsistent");
    return false;
  }
  row.processing_noted = true;
  row.processing_entered = processing_entered;
  row.processing_returned = processing_returned;
  row.updater_invoked = updater_invoked;
  row.state_row_emitted = state_row_emitted;
  if (processing_returned) {
    row.processing_status = "processed";
  } else {
    row.processing_status = trace_fatal ? "trace_failure" : "process_terminated";
  }
  if (state_row_emitted) {
    if (next_trajectory_index_ == std::numeric_limits<std::uint64_t>::max()) {
      Reject("trajectory index overflowed u64");
      return false;
    }
    row.trajectory_index_available = true;
    row.trajectory_index = next_trajectory_index_++;
    row.position_G = position_G;
    row.quaternion_ItoG_xyzw = quaternion_ItoG_xyzw;
  }
  return true;
}

bool ov_msckf::CP2SerialRuntimeTrace::NoteUpdate(
    const CP2LiveUpdateEvent &event) noexcept {
  if (!ready() || finalized_ || !event.invocation_context_available ||
      event.sequence_index != context_.sequence_index ||
      event.pair_index >= rows_.size() ||
      event.invocation_id != next_invocation_id_) {
    Reject("updater event identity is invalid or noncontiguous");
    return false;
  }
  if (context_.trace_level == CP2RuntimeTraceLevel::kTiming &&
      !timing_lifecycle_valid(event)) {
    Reject("timing updater lifecycle or endpoint is invalid");
    return false;
  }
  Row &row = rows_[static_cast<std::size_t>(event.pair_index)];
  if (!row.enqueue_noted || row.enqueue_status != "queued" ||
      event.camera_timestamp_ns != row.pair.camera_timestamp_ns ||
      !row.updates.empty()) {
    Reject("updater event does not join its queued serial pair");
    return false;
  }
  try {
    row.updater_invocation_ids.push_back(event.invocation_id);
    if (context_.trace_level == CP2RuntimeTraceLevel::kTiming) {
      UpdateRow retained;
      retained.invocation_id = event.invocation_id;
      retained.timing_start_ns = event.timing_start_ns;
      retained.timing_end_ns = event.timing_end_ns;
      retained.duration_ns = event.duration_ns;
      retained.input_feature_count = event.input_feature_count;
      retained.raw_system_count = event.raw_system_count;
      retained.terminal_status =
          cp2_update_terminal_status_name(event.terminal_status);
      retained.terminal_subreason =
          cp2_update_terminal_subreason_name(event.terminal_subreason);
      retained.nonempty = event.input_feature_count != 0U;
      retained.baseline_preflight_attempted =
          event.baseline_preflight_attempted;
      retained.preflight_accepted = event.baseline_preflight_accepted;
      retained.committed =
          event.terminal_status ==
              CP2UpdateTerminalStatus::kCommittedCounted &&
          event.baseline_commit_occurred;
      retained.primary = retained.nonempty && retained.preflight_accepted &&
                         retained.committed;
      row.updates.push_back(std::move(retained));
    }
  } catch (...) {
    Reject("updater identity allocation failed");
    return false;
  }
  if (next_invocation_id_ == std::numeric_limits<std::uint64_t>::max()) {
    Reject("updater invocation population overflowed u64");
    return false;
  }
  ++next_invocation_id_;
  return true;
}

bool ov_msckf::CP2SerialRuntimeTrace::SerializeSerial(
    std::string &output_bytes) const {
  std::ostringstream output;
  output.imbue(std::locale::classic());
  for (const Row &row : rows_) {
    const CP2SerialPair &pair = row.pair;
    if (context_.trace_level == CP2RuntimeTraceLevel::kRecordedFull) {
      output << "{\"absolute_record_delta_ns\":"
             << pair.absolute_record_delta_ns
             << ",\"anchor_camera_id\":" << pair.anchor_camera_id
             << ",\"anchor_filtered_index\":"
             << pair.anchor_filtered_index
             << ",\"cam0_filtered_index\":" << pair.cam0_filtered_index
             << ",\"cam0_header_time_ns\":" << pair.cam0_header_time_ns
             << ",\"cam0_record_time_ns\":" << pair.cam0_record_time_ns
             << ",\"cam1_filtered_index\":" << pair.cam1_filtered_index
             << ",\"cam1_header_time_ns\":" << pair.cam1_header_time_ns
             << ",\"cam1_record_time_ns\":" << pair.cam1_record_time_ns
             << ",\"camera_timestamp_ns\":" << pair.camera_timestamp_ns
             << ",\"enqueue_entered\":" << json_bool(row.enqueue_entered)
             << ",\"enqueue_returned\":" << json_bool(row.enqueue_returned)
             << ",\"enqueue_status\":" << json_string(row.enqueue_status)
             << ",\"pair_index\":" << pair.pair_index
             << ",\"processing_entered\":"
             << json_bool(row.processing_entered)
             << ",\"processing_returned\":"
             << json_bool(row.processing_returned)
             << ",\"processing_status\":"
             << json_string(row.processing_status)
             << ",\"record_type\":\"serial_pair\",\"schema_version\":1"
             << ",\"selected\":true,\"sequence_id\":"
             << json_string(context_.sequence_id)
             << ",\"sequence_index\":" << context_.sequence_index
             << ",\"updater_invocation_ids\":"
             << json_u64_array(row.updater_invocation_ids) << "}\n";
    } else {
      output << "{\"absolute_record_delta_ns\":"
             << pair.absolute_record_delta_ns
             << ",\"anchor_camera_id\":" << pair.anchor_camera_id
             << ",\"anchor_filtered_index\":"
             << pair.anchor_filtered_index
             << ",\"cam0_filtered_index\":" << pair.cam0_filtered_index
             << ",\"cam0_header_time_ns\":" << pair.cam0_header_time_ns
             << ",\"cam0_record_time_ns\":" << pair.cam0_record_time_ns
             << ",\"cam1_filtered_index\":" << pair.cam1_filtered_index
             << ",\"cam1_header_time_ns\":" << pair.cam1_header_time_ns
             << ",\"cam1_record_time_ns\":" << pair.cam1_record_time_ns
             << ",\"pair_index\":" << pair.pair_index
             << ",\"record_type\":\"pair_index\",\"schema_version\":1"
             << ",\"sequence_id\":" << json_string(context_.sequence_id)
             << ",\"sequence_index\":" << context_.sequence_index << "}\n";
    }
  }
  output_bytes = output.str();
  return true;
}

bool ov_msckf::CP2SerialRuntimeTrace::SerializeCallbacks(
    std::string &output_bytes) const {
  std::ostringstream output;
  output.imbue(std::locale::classic());
  for (std::size_t index = 0U; index < rows_.size(); ++index) {
    const Row &row = rows_[index];
    const CP2SerialPair &pair = row.pair;
    output << "{\"anchor_filtered_index\":" << pair.anchor_filtered_index
           << ",\"callback_index\":" << index
           << ",\"cam0_filtered_index\":" << pair.cam0_filtered_index
           << ",\"cam0_header_time_ns\":" << pair.cam0_header_time_ns
           << ",\"cam0_record_time_ns\":" << pair.cam0_record_time_ns
           << ",\"cam1_filtered_index\":" << pair.cam1_filtered_index
           << ",\"cam1_header_time_ns\":" << pair.cam1_header_time_ns
           << ",\"cam1_record_time_ns\":" << pair.cam1_record_time_ns
           << ",\"camera_timestamp_ns\":" << pair.camera_timestamp_ns
           << ",\"enqueue_entered\":" << json_bool(row.enqueue_entered)
           << ",\"enqueue_returned\":" << json_bool(row.enqueue_returned)
           << ",\"enqueue_status\":" << json_string(row.enqueue_status)
           << ",\"mode\":" << json_string(context_.mode)
           << ",\"pair_index\":" << pair.pair_index
           << ",\"processing_entered\":"
           << json_bool(row.processing_entered)
           << ",\"processing_returned\":"
           << json_bool(row.processing_returned)
           << ",\"processing_status\":"
           << json_string(row.processing_status)
           << ",\"record_type\":\"serial_callback\",\"schema_version\":1"
           << ",\"sequence_id\":" << json_string(context_.sequence_id)
           << ",\"sequence_index\":" << context_.sequence_index
           << ",\"state_row_emitted\":" << json_bool(row.state_row_emitted)
           << ",\"trajectory_index\":";
    if (row.trajectory_index_available) {
      output << row.trajectory_index;
    } else {
      output << "null";
    }
    if (context_.trace_level == CP2RuntimeTraceLevel::kTiming) {
      output << ",\"updater_invocation_ids\":"
             << json_u64_array(row.updater_invocation_ids)
             << ",\"updater_invoked\":" << json_bool(row.updater_invoked);
    }
    output << "}\n";
  }
  output_bytes = output.str();
  return true;
}

bool ov_msckf::CP2SerialRuntimeTrace::SerializeTrajectory(
    std::string &output_bytes) const {
  std::ostringstream output;
  output.imbue(std::locale::classic());
  output << std::setprecision(17);
  std::uint64_t next = 0U;
  std::uint64_t previous_timestamp = 0U;
  bool have_previous = false;
  for (std::size_t index = 0U; index < rows_.size(); ++index) {
    const Row &row = rows_[index];
    if (!row.state_row_emitted) {
      if (row.trajectory_index_available) {
        return false;
      }
      continue;
    }
    if (!row.trajectory_index_available || row.trajectory_index != next ||
        !finite_pose(row.position_G, row.quaternion_ItoG_xyzw) ||
        (have_previous &&
         row.pair.camera_timestamp_ns <= previous_timestamp)) {
      return false;
    }
    output << "{\"callback_index\":" << index
           << ",\"camera_timestamp_ns\":" << row.pair.camera_timestamp_ns
           << ",\"mode\":" << json_string(context_.mode)
           << ",\"pair_index\":" << row.pair.pair_index
           << ",\"position_G\":[" << row.position_G[0] << ','
           << row.position_G[1] << ',' << row.position_G[2]
           << "],\"quaternion_ItoG_xyzw\":["
           << row.quaternion_ItoG_xyzw[0] << ','
           << row.quaternion_ItoG_xyzw[1] << ','
           << row.quaternion_ItoG_xyzw[2] << ','
           << row.quaternion_ItoG_xyzw[3]
           << "],\"record_type\":\"trajectory_pose\",\"schema_version\":1"
           << ",\"sequence_id\":" << json_string(context_.sequence_id)
           << ",\"sequence_index\":" << context_.sequence_index
           << ",\"trajectory_index\":" << next << "}\n";
    previous_timestamp = row.pair.camera_timestamp_ns;
    have_previous = true;
    ++next;
  }
  if (next != next_trajectory_index_) {
    return false;
  }
  output_bytes = output.str();
  return true;
}

bool ov_msckf::CP2SerialRuntimeTrace::SerializeUpdater(
    std::string &output_bytes) const {
  std::ostringstream output;
  output.imbue(std::locale::classic());
  for (const Row &row : rows_) {
    for (const UpdateRow &update : row.updates) {
      output << "{\"baseline_preflight_attempted\":"
             << json_bool(update.baseline_preflight_attempted)
             << ",\"camera_timestamp_ns\":"
             << row.pair.camera_timestamp_ns
             << ",\"committed\":" << json_bool(update.committed)
             << ",\"duration_ns\":" << update.duration_ns
             << ",\"input_feature_count\":" << update.input_feature_count
             << ",\"invocation_id\":" << update.invocation_id
             << ",\"mode\":" << json_string(context_.mode)
             << ",\"nonempty\":" << json_bool(update.nonempty)
             << ",\"pair_index\":" << row.pair.pair_index
             << ",\"preflight_accepted\":"
             << json_bool(update.preflight_accepted)
             << ",\"primary\":" << json_bool(update.primary)
             << ",\"raw_system_count\":" << update.raw_system_count
             << ",\"record_type\":\"updater_event\",\"schema_version\":1"
             << ",\"sequence_id\":" << json_string(context_.sequence_id)
             << ",\"sequence_index\":" << context_.sequence_index
             << ",\"terminal_status\":"
             << json_string(update.terminal_status)
             << ",\"terminal_subreason\":"
             << json_string(update.terminal_subreason)
             << ",\"timer_clock\":\"std::chrono::steady_clock\""
             << ",\"timer_end_ns\":" << update.timing_end_ns
             << ",\"timer_start_ns\":" << update.timing_start_ns
             << "}\n";
    }
  }
  output_bytes = output.str();
  return true;
}

bool ov_msckf::CP2SerialRuntimeTrace::SerializeTiming(
    std::string &output_bytes) const {
  std::ostringstream output;
  output.imbue(std::locale::classic());
  for (const Row &row : rows_) {
    for (const UpdateRow &update : row.updates) {
      output << "{\"cam0_record_time_ns\":"
             << row.pair.cam0_record_time_ns
             << ",\"camera_timestamp_ns\":"
             << row.pair.camera_timestamp_ns
             << ",\"committed\":" << json_bool(update.committed)
             << ",\"duration_ns\":" << update.duration_ns
             << ",\"invocation_id\":" << update.invocation_id
             << ",\"mode\":" << json_string(context_.mode)
             << ",\"nonempty\":" << json_bool(update.nonempty)
             << ",\"preflight_accepted\":"
             << json_bool(update.preflight_accepted)
             << ",\"primary\":" << json_bool(update.primary)
             << ",\"record_type\":\"updater_timing\",\"schema_version\":1"
             << ",\"sequence_id\":" << json_string(context_.sequence_id)
             << ",\"sequence_index\":" << context_.sequence_index
             << ",\"serial_pair_index\":" << row.pair.pair_index
             << ",\"terminal_status\":"
             << json_string(update.terminal_status)
             << ",\"timer_clock\":\"std::chrono::steady_clock\""
             << ",\"timer_end_ns\":" << update.timing_end_ns
             << ",\"timer_start_ns\":" << update.timing_start_ns
             << "}\n";
    }
  }
  output_bytes = output.str();
  return true;
}

bool ov_msckf::CP2SerialRuntimeTrace::Finalize() noexcept {
  if (!ready() || finalized_ || !context_.serial_trace_path.available) {
    Reject("serial trace finalization state is invalid");
    return false;
  }
  try {
    for (const Row &row : rows_) {
      if (!row.enqueue_noted) {
        Reject("selected serial pair was never passed to the callback");
        return false;
      }
      if ((row.enqueue_status == "queued") !=
          (row.processing_status == "processed" ||
           row.processing_status == "queued_unprocessed" ||
           row.processing_status == "process_terminated" ||
           row.processing_status == "trace_failure")) {
        Reject("serial enqueue/processing transition is invalid");
        return false;
      }
      if (row.processing_status == "processed" && !row.processing_noted) {
        Reject("processed serial pair lacks a processing event");
        return false;
      }
      if (context_.trace_level == CP2RuntimeTraceLevel::kTiming &&
          (row.updater_invocation_ids.size() != row.updates.size() ||
           (!row.updates.empty() && !row.processing_noted))) {
        Reject("serial updater event population is incomplete");
        return false;
      }
      for (std::size_t index = 0U; index < row.updates.size(); ++index) {
        if (row.updater_invocation_ids[index] !=
            row.updates[index].invocation_id) {
          Reject("serial updater identity population is inconsistent");
          return false;
        }
      }
      if (row.updater_invoked != !row.updater_invocation_ids.empty()) {
        Reject("serial updater-invoked flag disagrees with invocation joins");
        return false;
      }
    }

    std::string serial;
    if (!SerializeSerial(serial) ||
        !WriteOutput(context_.serial_trace_path,
                     output_capabilities_.serial_trace, serial)) {
      Reject("serial trace file could not be created");
      return false;
    }
    if (context_.trace_level == CP2RuntimeTraceLevel::kSequence) {
      if (!context_.callback_trace_path.available ||
          !context_.trajectory_trace_path.available) {
        Reject("sequence trace paths are unavailable");
        return false;
      }
      std::string callbacks;
      std::string trajectory;
      if (!SerializeCallbacks(callbacks) ||
          !SerializeTrajectory(trajectory) ||
          !WriteOutput(context_.callback_trace_path,
                       output_capabilities_.callback_trace, callbacks) ||
          !WriteOutput(context_.trajectory_trace_path,
                       output_capabilities_.trajectory_trace, trajectory)) {
        Reject("sequence callback/trajectory files could not be created");
        return false;
      }
    } else if (context_.trace_level == CP2RuntimeTraceLevel::kTiming) {
      if (!context_.callback_trace_path.available ||
          !context_.updater_trace_path.available ||
          !context_.timing_trace_path.available) {
        Reject("timing trace paths are unavailable");
        return false;
      }
      std::string callbacks;
      std::string updater;
      std::string timing;
      if (!SerializeCallbacks(callbacks) || !SerializeUpdater(updater) ||
          !SerializeTiming(timing) ||
          !WriteOutput(context_.callback_trace_path,
                       output_capabilities_.callback_trace, callbacks) ||
          !WriteOutput(context_.updater_trace_path,
                       output_capabilities_.updater_trace, updater) ||
          !WriteOutput(context_.timing_trace_path,
                       output_capabilities_.timing_trace, timing)) {
        Reject("timing callback/updater/sample files could not be created");
        return false;
      }
    }
    finalized_ = true;
    return true;
  } catch (...) {
    Reject("serial trace serialization failed");
    return false;
  }
}

bool ov_msckf::CP2SerialRuntimeTrace::WriteOutput(
    const CP2NullablePath &path,
    const CP2OutputCapability &capability,
    const std::string &bytes) noexcept {
  if (!path.available) {
    return false;
  }
  return held_capability_mode_
             ? WriteCP2OutputCapability(path.value, capability, bytes)
             : WriteNewFile(path.value, bytes);
}

bool ov_msckf::CP2SerialRuntimeTrace::ReadLoaderMap(
    std::string &output) noexcept {
  try {
    const ScopedDescriptor descriptor(
        ::open("/proc/self/maps", O_RDONLY | O_CLOEXEC | O_NOFOLLOW));
    if (descriptor.get() < 0) {
      return false;
    }
    constexpr std::size_t kMaximumBytes = 16U * 1024U * 1024U;
    std::array<char, 8192U> buffer{};
    output.clear();
    while (true) {
      const ssize_t count =
          ::read(descriptor.get(), buffer.data(), buffer.size());
      if (count < 0 && errno == EINTR) {
        continue;
      }
      if (count < 0) {
        output.clear();
        return false;
      }
      if (count == 0) {
        return true;
      }
      const std::size_t amount = static_cast<std::size_t>(count);
      if (amount > kMaximumBytes - output.size()) {
        output.clear();
        return false;
      }
      output.append(buffer.data(), amount);
    }
  } catch (...) {
    output.clear();
    return false;
  }
}

bool ov_msckf::CP2SerialRuntimeTrace::WriteNewFile(
    const std::string &path, const std::string &bytes) noexcept {
  const std::size_t separator = path.rfind('/');
  if (path.empty() || path.front() != '/' || separator == std::string::npos ||
      separator == 0U || separator + 1U >= path.size()) {
    return false;
  }
  const std::string parent_path = path.substr(0U, separator);
  const std::string leaf = path.substr(separator + 1U);
  if (leaf == "." || leaf == "..") {
    return false;
  }
  const ScopedDescriptor parent(
      ::open(parent_path.c_str(), O_RDONLY | O_DIRECTORY | O_NOFOLLOW |
                                      O_CLOEXEC));
  if (parent.get() < 0) {
    return false;
  }
  struct stat parent_identity {};
  struct stat parent_by_path {};
  if (::fstat(parent.get(), &parent_identity) != 0 ||
      ::lstat(parent_path.c_str(), &parent_by_path) != 0 ||
      !S_ISDIR(parent_identity.st_mode) ||
      parent_identity.st_uid != ::geteuid() ||
      (parent_identity.st_mode & 0777U) != 0700U ||
      parent_identity.st_dev != parent_by_path.st_dev ||
      parent_identity.st_ino != parent_by_path.st_ino) {
    return false;
  }
  const ScopedDescriptor descriptor(
      ::openat(parent.get(), leaf.c_str(),
               O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW,
               S_IRUSR | S_IWUSR));
  if (descriptor.get() < 0) {
    return false;
  }
  struct stat opened {};
  if (::fstat(descriptor.get(), &opened) != 0 ||
      !S_ISREG(opened.st_mode) || opened.st_uid != ::geteuid() ||
      opened.st_nlink != 1U ||
      !write_all(descriptor.get(),
                 reinterpret_cast<const std::uint8_t *>(bytes.data()),
                 bytes.size()) ||
      ::fchmod(descriptor.get(), S_IRUSR | S_IRGRP | S_IROTH) != 0 ||
      ::fsync(descriptor.get()) != 0) {
    return false;
  }
  struct stat retained {};
  struct stat by_name {};
  struct stat parent_after {};
  struct stat parent_path_after {};
  if (::fstat(descriptor.get(), &retained) != 0 ||
      ::fstatat(parent.get(), leaf.c_str(), &by_name,
                AT_SYMLINK_NOFOLLOW) != 0 ||
      !S_ISREG(retained.st_mode) || retained.st_nlink != 1U ||
      retained.st_uid != ::geteuid() ||
      (retained.st_mode & 0777U) != 0444U ||
      retained.st_size < 0 ||
      static_cast<std::uintmax_t>(retained.st_size) !=
          static_cast<std::uintmax_t>(bytes.size()) ||
      retained.st_dev != by_name.st_dev || retained.st_ino != by_name.st_ino ||
      retained.st_mode != by_name.st_mode ||
      retained.st_nlink != by_name.st_nlink ||
      retained.st_uid != by_name.st_uid || retained.st_gid != by_name.st_gid ||
      retained.st_size != by_name.st_size || ::fsync(parent.get()) != 0 ||
      ::fstat(parent.get(), &parent_after) != 0 ||
      ::lstat(parent_path.c_str(), &parent_path_after) != 0 ||
      parent_identity.st_dev != parent_after.st_dev ||
      parent_identity.st_ino != parent_after.st_ino ||
      parent_after.st_dev != parent_path_after.st_dev ||
      parent_after.st_ino != parent_path_after.st_ino ||
      !S_ISDIR(parent_after.st_mode) || parent_after.st_uid != ::geteuid() ||
      (parent_after.st_mode & 0777U) != 0700U) {
    return false;
  }
  return true;
}
