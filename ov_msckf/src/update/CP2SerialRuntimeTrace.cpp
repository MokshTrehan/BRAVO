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
  Row &row = rows_[static_cast<std::size_t>(event.pair_index)];
  if (!row.enqueue_noted || row.enqueue_status != "queued" ||
      event.camera_timestamp_ns != row.pair.camera_timestamp_ns) {
    Reject("updater event does not join its queued serial pair");
    return false;
  }
  try {
    row.updater_invocation_ids.push_back(event.invocation_id);
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
      if (row.updater_invoked != !row.updater_invocation_ids.empty()) {
        Reject("serial updater-invoked flag disagrees with invocation joins");
        return false;
      }
    }

    std::string serial;
    if (!SerializeSerial(serial) ||
        !WriteNewFile(context_.serial_trace_path.value, serial)) {
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
          !WriteNewFile(context_.callback_trace_path.value, callbacks) ||
          !WriteNewFile(context_.trajectory_trace_path.value, trajectory)) {
        Reject("sequence callback/trajectory files could not be created");
        return false;
      }
    } else if (context_.trace_level == CP2RuntimeTraceLevel::kTiming) {
      // CP2-E remains preregistered and blocked until an exact replacement
      // schema is committed.  Refuse to manufacture a timing-evidence sink.
      Reject("timing evidence schema is not frozen");
      return false;
    }
    finalized_ = true;
    return true;
  } catch (...) {
    Reject("serial trace serialization failed");
    return false;
  }
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
  const ScopedDescriptor descriptor(
      ::open(path.c_str(), O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC |
                               O_NOFOLLOW,
             S_IRUSR | S_IWUSR));
  if (descriptor.get() < 0) {
    return false;
  }
  struct stat identity {};
  return ::fstat(descriptor.get(), &identity) == 0 &&
         S_ISREG(identity.st_mode) && identity.st_nlink == 1U &&
         write_all(descriptor.get(),
                   reinterpret_cast<const std::uint8_t *>(bytes.data()),
                   bytes.size()) &&
         ::fsync(descriptor.get()) == 0;
}
