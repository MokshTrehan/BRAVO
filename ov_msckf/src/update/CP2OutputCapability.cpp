/*
 * SchurVIO-Lite CP2 held-inode output capabilities.
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include "CP2OutputCapability.h"

#include <array>
#include <cerrno>
#include <climits>
#include <fcntl.h>
#include <limits>
#include <set>
#include <sys/stat.h>
#include <type_traits>
#include <unistd.h>
#include <utility>

namespace {

constexpr std::uint64_t kNanosecondsPerSecond = UINT64_C(1000000000);

bool parse_decimal_u64(const std::string &value,
                       std::uint64_t &output) noexcept {
  output = 0U;
  if (value.empty() || (value.size() > 1U && value.front() == '0')) {
    return false;
  }
  for (char byte : value) {
    if (byte < '0' || byte > '9') {
      output = 0U;
      return false;
    }
    const std::uint64_t digit = static_cast<std::uint64_t>(byte - '0');
    if (output > (std::numeric_limits<std::uint64_t>::max() - digit) /
                     UINT64_C(10)) {
      output = 0U;
      return false;
    }
    output = output * UINT64_C(10) + digit;
  }
  return true;
}

template <typename Value>
bool value_to_u64(Value value, std::uint64_t &output) noexcept {
  static_assert(std::is_integral<Value>::value,
                "CP2 stat identity fields must be integral");
  output = 0U;
  if (std::numeric_limits<Value>::is_signed && value < Value{0}) {
    return false;
  }
  const std::uintmax_t converted = static_cast<std::uintmax_t>(value);
  if (converted > std::numeric_limits<std::uint64_t>::max()) {
    return false;
  }
  output = static_cast<std::uint64_t>(converted);
  return true;
}

template <typename Value>
bool u64_fits(std::uint64_t value) noexcept {
  static_assert(std::is_integral<Value>::value,
                "CP2 capability target type must be integral");
  return value <=
         static_cast<std::uintmax_t>(std::numeric_limits<Value>::max());
}

bool timespec_ns(const struct timespec &value,
                 std::uint64_t &output) noexcept {
  output = 0U;
  std::uint64_t seconds = 0U;
  std::uint64_t nanoseconds = 0U;
  if (!value_to_u64(value.tv_sec, seconds) ||
      !value_to_u64(value.tv_nsec, nanoseconds) ||
      nanoseconds >= kNanosecondsPerSecond ||
      seconds > (std::numeric_limits<std::uint64_t>::max() - nanoseconds) /
                    kNanosecondsPerSecond) {
    return false;
  }
  output = seconds * kNanosecondsPerSecond + nanoseconds;
  return true;
}

bool stat_fields(const struct stat &status,
                 ov_msckf::CP2OutputCapability &output) noexcept {
  return value_to_u64(status.st_dev, output.device) &&
         value_to_u64(status.st_ino, output.inode) &&
         value_to_u64(status.st_mode, output.mode) &&
         value_to_u64(status.st_nlink, output.link_count) &&
         value_to_u64(status.st_uid, output.uid) &&
         value_to_u64(status.st_gid, output.gid) &&
         value_to_u64(status.st_size, output.size) &&
         timespec_ns(status.st_mtim, output.mtime_ns) &&
         timespec_ns(status.st_ctim, output.ctime_ns);
}

bool initial_stat_matches(
    const struct stat &status,
    const ov_msckf::CP2OutputCapability &expected) noexcept {
  ov_msckf::CP2OutputCapability observed;
  if (!stat_fields(status, observed)) {
    return false;
  }
  return observed.device == expected.device &&
         observed.inode == expected.inode && observed.mode == expected.mode &&
         observed.link_count == expected.link_count &&
         observed.uid == expected.uid && observed.gid == expected.gid &&
         observed.size == expected.size &&
         observed.mtime_ns == expected.mtime_ns &&
         observed.ctime_ns == expected.ctime_ns;
}

bool same_final_file(const struct stat &left,
                     const struct stat &right) noexcept {
  return left.st_dev == right.st_dev && left.st_ino == right.st_ino &&
         left.st_mode == right.st_mode && left.st_nlink == right.st_nlink &&
         left.st_uid == right.st_uid && left.st_gid == right.st_gid &&
         left.st_size == right.st_size;
}

bool parent_is_private(const struct stat &status) noexcept {
  return S_ISDIR(status.st_mode) && status.st_uid == ::geteuid() &&
         (status.st_mode & 07777U) == 0700U;
}

bool split_canonical_path(const std::string &path, std::string &parent,
                          std::string &leaf) {
  const std::size_t separator = path.rfind('/');
  if (path.empty() || path.front() != '/' ||
      separator == std::string::npos || separator == 0U ||
      separator + 1U >= path.size()) {
    return false;
  }
  parent = path.substr(0U, separator);
  leaf = path.substr(separator + 1U);
  return leaf != "." && leaf != ".." &&
         leaf.find('/') == std::string::npos &&
         leaf.find('\0') == std::string::npos;
}

bool parse_proc_fd_path(const std::string &path) noexcept {
  try {
    constexpr char prefix[] = "/proc/";
    constexpr char separator[] = "/fd/";
    if (path.compare(0U, sizeof(prefix) - 1U, prefix) != 0) {
      return false;
    }
    const std::size_t split = path.find(separator, sizeof(prefix) - 1U);
    if (split == std::string::npos ||
        path.find('/', split + sizeof(separator) - 1U) !=
            std::string::npos) {
      return false;
    }
    std::uint64_t pid = 0U;
    std::uint64_t descriptor = 0U;
    return parse_decimal_u64(
               path.substr(sizeof(prefix) - 1U,
                           split - (sizeof(prefix) - 1U)), pid) &&
           parse_decimal_u64(
               path.substr(split + sizeof(separator) - 1U), descriptor) &&
           pid > 0U && u64_fits<pid_t>(pid) && descriptor >= 3U &&
           descriptor <= static_cast<std::uint64_t>(INT_MAX);
  } catch (...) {
    return false;
  }
}

bool sync_descriptor(int descriptor) noexcept {
  int status = -1;
  do {
    status = ::fsync(descriptor);
  } while (status != 0 && errno == EINTR);
  return status == 0;
}

bool write_all(int descriptor, const std::uint8_t *bytes,
               std::size_t size) noexcept {
  std::size_t offset = 0U;
  while (offset < size) {
    const std::size_t maximum = static_cast<std::size_t>(
        std::numeric_limits<ssize_t>::max());
    const std::size_t requested =
        size - offset < maximum ? size - offset : maximum;
    ssize_t count = -1;
    do {
      count = ::write(descriptor, bytes + offset, requested);
    } while (count < 0 && errno == EINTR);
    if (count <= 0) {
      return false;
    }
    offset += static_cast<std::size_t>(count);
  }
  return true;
}

bool unavailable_is_zero(
    const ov_msckf::CP2OutputCapability &capability) noexcept {
  return !capability.available && capability.runner_pid == 0U &&
         capability.descriptor == 0U && capability.device == 0U &&
         capability.inode == 0U && capability.mode == 0U &&
         capability.link_count == 0U && capability.uid == 0U &&
         capability.gid == 0U && capability.size == 0U &&
         capability.mtime_ns == 0U && capability.ctime_ns == 0U;
}

bool capability_shape_valid(
    const ov_msckf::CP2OutputCapability &capability) noexcept {
  if (!capability.available) {
    return unavailable_is_zero(capability);
  }
  // The trusted CP2-C campaign worker is deliberately PID 1 inside its
  // private PID namespace.  PID 0 is never a userspace process, but PID 1 is
  // a valid descriptor holder and must remain addressable through that
  // namespace's /proc/1/fd population.
  return capability.runner_pid > 0U &&
         u64_fits<pid_t>(capability.runner_pid) &&
         capability.descriptor >= 3U &&
         capability.descriptor <= static_cast<std::uint64_t>(INT_MAX) &&
         capability.inode != 0U &&
         capability.mode == static_cast<std::uint64_t>(S_IFREG | 0600U) &&
         capability.link_count == 1U &&
         capability.uid == static_cast<std::uint64_t>(::geteuid()) &&
         capability.size == 0U;
}

} // namespace

ov_msckf::CP2OpenedOutput::~CP2OpenedOutput() noexcept { Reset(); }

ov_msckf::CP2OpenedOutput::CP2OpenedOutput(
    CP2OpenedOutput &&other) noexcept
    : descriptor_(other.descriptor_),
      parent_descriptor_(other.parent_descriptor_),
      parent_path_(std::move(other.parent_path_)),
      leaf_(std::move(other.leaf_)), capability_(other.capability_) {
  other.descriptor_ = -1;
  other.parent_descriptor_ = -1;
  other.capability_ = CP2OutputCapability{};
}

ov_msckf::CP2OpenedOutput &ov_msckf::CP2OpenedOutput::operator=(
    CP2OpenedOutput &&other) noexcept {
  if (this != &other) {
    Reset();
    descriptor_ = other.descriptor_;
    parent_descriptor_ = other.parent_descriptor_;
    parent_path_ = std::move(other.parent_path_);
    leaf_ = std::move(other.leaf_);
    capability_ = other.capability_;
    other.descriptor_ = -1;
    other.parent_descriptor_ = -1;
    other.capability_ = CP2OutputCapability{};
  }
  return *this;
}

void ov_msckf::CP2OpenedOutput::Reset() noexcept {
  if (descriptor_ >= 0) {
    (void)::close(descriptor_);
  }
  if (parent_descriptor_ >= 0) {
    (void)::close(parent_descriptor_);
  }
  descriptor_ = -1;
  parent_descriptor_ = -1;
  parent_path_.clear();
  leaf_.clear();
  capability_ = CP2OutputCapability{};
}

bool ov_msckf::ParseCP2OutputCapability(
    const std::string &encoded, CP2OutputCapability &output) noexcept {
  output = CP2OutputCapability{};
  if (encoded == "null") {
    return true;
  }
  try {
    std::array<std::string, 12U> fields;
    std::size_t beginning = 0U;
    for (std::size_t index = 0U; index < fields.size(); ++index) {
      const std::size_t separator = encoded.find(':', beginning);
      if ((index + 1U < fields.size() && separator == std::string::npos) ||
          (index + 1U == fields.size() && separator != std::string::npos)) {
        return false;
      }
      const std::size_t ending = separator == std::string::npos
                                     ? encoded.size()
                                     : separator;
      fields[index] = encoded.substr(beginning, ending - beginning);
      beginning = ending + 1U;
    }
    if (fields[0] != "v1") {
      return false;
    }
    CP2OutputCapability parsed;
    parsed.available = true;
    std::array<std::uint64_t *, 11U> destinations = {{
        &parsed.runner_pid, &parsed.descriptor, &parsed.device,
        &parsed.inode, &parsed.mode, &parsed.link_count, &parsed.uid,
        &parsed.gid, &parsed.size, &parsed.mtime_ns, &parsed.ctime_ns,
    }};
    for (std::size_t index = 0U; index < destinations.size(); ++index) {
      if (!parse_decimal_u64(fields[index + 1U], *destinations[index])) {
        return false;
      }
    }
    if (!capability_shape_valid(parsed)) {
      return false;
    }
    output = parsed;
    return true;
  } catch (...) {
    output = CP2OutputCapability{};
    return false;
  }
}

bool ov_msckf::CP2OutputCapabilityPath(
    const CP2OutputCapability &capability, std::string &output) noexcept {
  output.clear();
  if (!capability_shape_valid(capability) || !capability.available) {
    return false;
  }
  try {
    output = "/proc/" + std::to_string(capability.runner_pid) + "/fd/" +
             std::to_string(capability.descriptor);
    return true;
  } catch (...) {
    output.clear();
    return false;
  }
}

bool ov_msckf::ValidateCP2OutputCapabilityBinding(
    const std::string &canonical_path,
    const CP2OutputCapability &capability) noexcept {
  int parent_descriptor = -1;
  try {
    if (!capability_shape_valid(capability) || !capability.available) {
      return false;
    }
    std::string parent_path;
    std::string leaf;
    if (!split_canonical_path(canonical_path, parent_path, leaf)) {
      return false;
    }
    parent_descriptor = ::open(parent_path.c_str(),
                               O_RDONLY | O_DIRECTORY | O_NOFOLLOW |
                                   O_CLOEXEC);
    struct stat parent_status {};
    struct stat parent_by_path {};
    struct stat by_name {};
    if (parent_descriptor < 0 ||
        ::fstat(parent_descriptor, &parent_status) != 0 ||
        ::lstat(parent_path.c_str(), &parent_by_path) != 0 ||
        !parent_is_private(parent_status) ||
        parent_status.st_dev != parent_by_path.st_dev ||
        parent_status.st_ino != parent_by_path.st_ino ||
        ::fstatat(parent_descriptor, leaf.c_str(), &by_name,
                  AT_SYMLINK_NOFOLLOW) != 0 ||
        !initial_stat_matches(by_name, capability)) {
      if (parent_descriptor >= 0) {
        (void)::close(parent_descriptor);
      }
      return false;
    }
    std::string proc_path;
    struct stat by_capability {};
    const bool accepted =
        CP2OutputCapabilityPath(capability, proc_path) &&
        ::stat(proc_path.c_str(), &by_capability) == 0 &&
        initial_stat_matches(by_capability, capability) &&
        same_final_file(by_name, by_capability);
    const int close_status = ::close(parent_descriptor);
    parent_descriptor = -1;
    return accepted && close_status == 0;
  } catch (...) {
    if (parent_descriptor >= 0) {
      (void)::close(parent_descriptor);
    }
    return false;
  }
}

bool ov_msckf::OpenCP2PreopenedLegacyStream(
    const std::string &canonical_path,
    const std::string &capability_path,
    const CP2OutputCapability &capability,
    std::ofstream &output) noexcept {
  try {
    std::string expected_path;
    if (output.is_open() || !parse_proc_fd_path(capability_path) ||
        !ValidateCP2OutputCapabilityBinding(canonical_path, capability) ||
        !CP2OutputCapabilityPath(capability, expected_path) ||
        capability_path != expected_path) {
      return false;
    }
    struct stat before {};
    if (::stat(capability_path.c_str(), &before) != 0 ||
        !initial_stat_matches(before, capability) ||
        !S_ISREG(before.st_mode) || before.st_nlink != 1 ||
        before.st_uid != ::geteuid() || (before.st_mode & 07777U) != 0600U ||
        before.st_size != 0) {
      return false;
    }
    // `app` is intentional: unlike the historical ordinary-path branch it
    // never requests O_TRUNC, so the already capability-bound inode is only
    // extended by the estimator's contracted bytes.
    output.open(capability_path.c_str(),
                std::ofstream::out | std::ofstream::app);
    if (!output.is_open() || !output.good()) {
      output.close();
      return false;
    }
    struct stat after {};
    if (::stat(capability_path.c_str(), &after) != 0 ||
        !initial_stat_matches(after, capability) ||
        !same_final_file(before, after)) {
      output.close();
      return false;
    }
    return true;
  } catch (...) {
    if (output.is_open()) {
      output.close();
    }
    return false;
  }
}

bool ov_msckf::ValidateCP2RuntimeOutputCapabilities(
    const CP2RuntimeContext &context,
    const CP2RuntimeOutputCapabilities &capabilities) noexcept {
  const std::array<const CP2OutputCapability *, 14U> population = {{
      &capabilities.serial_trace, &capabilities.callback_trace,
      &capabilities.trajectory_trace, &capabilities.updater_trace,
      &capabilities.state_payload, &capabilities.proposal_payload,
      &capabilities.raw_system_payload, &capabilities.timing_trace,
      &capabilities.runtime_parameters, &capabilities.loader_map_before,
      &capabilities.loader_map_after, &capabilities.legacy_state,
      &capabilities.legacy_deviation, &capabilities.legacy_timing,
  }};
  std::array<bool, 14U> required{};
  required[0] = true;
  required[8] = true;
  required[9] = true;
  required[10] = true;
  if (context.trace_level == CP2RuntimeTraceLevel::kRecordedFull) {
    required[3] = true;
  } else if (context.trace_level == CP2RuntimeTraceLevel::kSequence) {
    required[1] = true;
    required[2] = true;
    required[11] = true;
    required[12] = true;
    required[13] = true;
  } else if (context.trace_level == CP2RuntimeTraceLevel::kTiming) {
    required[1] = true;
    required[3] = true;
    required[7] = true;
  } else {
    return false;
  }

  const std::array<const CP2NullablePath *, 14U> context_paths = {{
      &context.serial_trace_path, &context.callback_trace_path,
      &context.trajectory_trace_path, &context.updater_trace_path,
      &context.state_payload_path, &context.proposal_payload_path,
      &context.raw_system_payload_path, &context.timing_trace_path,
      &context.runtime_parameters_path, &context.loader_map_before_path,
      &context.loader_map_after_path, &context.legacy_state_path,
      &context.legacy_deviation_path, &context.legacy_timing_path,
  }};
  std::set<std::uint64_t> descriptors;
  std::set<std::pair<std::uint64_t, std::uint64_t>> inodes;
  std::uint64_t runner_pid = 0U;
  for (std::size_t index = 0U; index < population.size(); ++index) {
    const CP2OutputCapability &capability = *population[index];
    if (capability.available != required[index] ||
        !capability_shape_valid(capability) ||
        (required[index] && !context_paths[index]->available)) {
      return false;
    }
    if (!capability.available) {
      continue;
    }
    if ((runner_pid != 0U && capability.runner_pid != runner_pid) ||
        !descriptors.insert(capability.descriptor).second ||
        !inodes.emplace(capability.device, capability.inode).second) {
      return false;
    }
    runner_pid = capability.runner_pid;
  }
  return runner_pid != 0U;
}

bool ov_msckf::OpenCP2OutputCapability(
    const std::string &canonical_path,
    const CP2OutputCapability &capability,
    CP2OpenedOutput &output) noexcept {
  output.Reset();
  int parent_descriptor = -1;
  int descriptor = -1;
  try {
    if (!capability_shape_valid(capability) || !capability.available) {
      return false;
    }
    std::string parent_path;
    std::string leaf;
    if (!split_canonical_path(canonical_path, parent_path, leaf)) {
      return false;
    }
    parent_descriptor = ::open(parent_path.c_str(),
                               O_RDONLY | O_DIRECTORY | O_NOFOLLOW |
                                   O_CLOEXEC);
    struct stat parent_status {};
    struct stat parent_by_path {};
    if (parent_descriptor < 0 ||
        ::fstat(parent_descriptor, &parent_status) != 0 ||
        ::lstat(parent_path.c_str(), &parent_by_path) != 0 ||
        !parent_is_private(parent_status) ||
        parent_status.st_dev != parent_by_path.st_dev ||
        parent_status.st_ino != parent_by_path.st_ino) {
      if (parent_descriptor >= 0) {
        (void)::close(parent_descriptor);
      }
      return false;
    }

    struct stat by_name {};
    if (::fstatat(parent_descriptor, leaf.c_str(), &by_name,
                  AT_SYMLINK_NOFOLLOW) != 0 ||
        !initial_stat_matches(by_name, capability)) {
      (void)::close(parent_descriptor);
      return false;
    }
    std::string proc_path;
    if (!CP2OutputCapabilityPath(capability, proc_path)) {
      (void)::close(parent_descriptor);
      return false;
    }
    descriptor = ::open(proc_path.c_str(), O_WRONLY | O_CLOEXEC | O_NOCTTY);
    struct stat by_descriptor {};
    const int flags = descriptor >= 0 ? ::fcntl(descriptor, F_GETFL) : -1;
    const off_t offset = descriptor >= 0 ? ::lseek(descriptor, 0, SEEK_CUR) : -1;
    if (descriptor < 0 || flags < 0 || (flags & O_ACCMODE) == O_RDONLY ||
        offset != 0 || ::fstat(descriptor, &by_descriptor) != 0 ||
        !initial_stat_matches(by_descriptor, capability) ||
        !same_final_file(by_descriptor, by_name) ||
        !S_ISREG(by_descriptor.st_mode) || by_descriptor.st_nlink != 1 ||
        by_descriptor.st_uid != ::geteuid() ||
        (by_descriptor.st_mode & 07777U) != 0600U ||
        by_descriptor.st_size != 0) {
      if (descriptor >= 0) {
        (void)::close(descriptor);
      }
      (void)::close(parent_descriptor);
      return false;
    }

    CP2OpenedOutput candidate;
    candidate.descriptor_ = descriptor;
    candidate.parent_descriptor_ = parent_descriptor;
    candidate.parent_path_ = std::move(parent_path);
    candidate.leaf_ = std::move(leaf);
    candidate.capability_ = capability;
    descriptor = -1;
    parent_descriptor = -1;
    output = std::move(candidate);
    return true;
  } catch (...) {
    if (descriptor >= 0) {
      (void)::close(descriptor);
    }
    if (parent_descriptor >= 0) {
      (void)::close(parent_descriptor);
    }
    output.Reset();
    return false;
  }
}

bool ov_msckf::SealCP2OutputCapability(
    CP2OpenedOutput &output, std::uint64_t expected_size) noexcept {
  if (!output.available() || !u64_fits<off_t>(expected_size)) {
    return false;
  }
  struct stat before {};
  const off_t offset = ::lseek(output.descriptor_, 0, SEEK_CUR);
  if (offset < 0 || static_cast<std::uint64_t>(offset) != expected_size ||
      ::fstat(output.descriptor_, &before) != 0 ||
      !S_ISREG(before.st_mode) || before.st_nlink != 1 ||
      before.st_uid != ::geteuid() || before.st_size < 0 ||
      static_cast<std::uint64_t>(before.st_size) != expected_size ||
      static_cast<std::uint64_t>(before.st_dev) !=
          output.capability_.device ||
      static_cast<std::uint64_t>(before.st_ino) !=
          output.capability_.inode ||
      static_cast<std::uint64_t>(before.st_gid) != output.capability_.gid ||
      ::fchmod(output.descriptor_, S_IRUSR | S_IRGRP | S_IROTH) != 0 ||
      !sync_descriptor(output.descriptor_)) {
    return false;
  }
  struct stat retained {};
  struct stat by_name {};
  struct stat parent_status {};
  struct stat parent_by_path {};
  if (::fstat(output.descriptor_, &retained) != 0 ||
      ::fstatat(output.parent_descriptor_, output.leaf_.c_str(), &by_name,
                AT_SYMLINK_NOFOLLOW) != 0 ||
      !S_ISREG(retained.st_mode) || retained.st_nlink != 1 ||
      retained.st_uid != ::geteuid() ||
      (retained.st_mode & 07777U) != 0444U || retained.st_size < 0 ||
      static_cast<std::uint64_t>(retained.st_size) != expected_size ||
      static_cast<std::uint64_t>(retained.st_dev) !=
          output.capability_.device ||
      static_cast<std::uint64_t>(retained.st_ino) !=
          output.capability_.inode ||
      static_cast<std::uint64_t>(retained.st_gid) !=
          output.capability_.gid ||
      !same_final_file(retained, by_name) ||
      !sync_descriptor(output.parent_descriptor_) ||
      ::fstat(output.parent_descriptor_, &parent_status) != 0 ||
      ::lstat(output.parent_path_.c_str(), &parent_by_path) != 0 ||
      !parent_is_private(parent_status) ||
      parent_status.st_dev != parent_by_path.st_dev ||
      parent_status.st_ino != parent_by_path.st_ino) {
    return false;
  }
  return true;
}

bool ov_msckf::WriteCP2OutputCapability(
    const std::string &canonical_path,
    const CP2OutputCapability &capability,
    const std::string &bytes) noexcept {
  if (bytes.size() >
      static_cast<std::size_t>(std::numeric_limits<std::uint64_t>::max())) {
    return false;
  }
  CP2OpenedOutput output;
  return OpenCP2OutputCapability(canonical_path, capability, output) &&
         write_all(output.descriptor(),
                   reinterpret_cast<const std::uint8_t *>(bytes.data()),
                   bytes.size()) &&
         SealCP2OutputCapability(
             output, static_cast<std::uint64_t>(bytes.size()));
}
