/*
 * SchurVIO-Lite CP2 held-inode output capabilities.
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#ifndef OV_MSCKF_CP2_OUTPUT_CAPABILITY_H
#define OV_MSCKF_CP2_OUTPUT_CAPABILITY_H

#include "CP2RuntimeContext.h"

#include <cstdint>
#include <fstream>
#include <string>

namespace ov_msckf {

/** Exact initial stat identity of one parent-precreated output inode. */
struct CP2OutputCapability {
  bool available = false;
  std::uint64_t runner_pid = 0U;
  std::uint64_t descriptor = 0U;
  std::uint64_t device = 0U;
  std::uint64_t inode = 0U;
  std::uint64_t mode = 0U;
  std::uint64_t link_count = 0U;
  std::uint64_t uid = 0U;
  std::uint64_t gid = 0U;
  std::uint64_t size = 0U;
  std::uint64_t mtime_ns = 0U;
  std::uint64_t ctime_ns = 0U;
};

/** Closed capability population matching the fourteen runtime-context paths. */
struct CP2RuntimeOutputCapabilities {
  CP2OutputCapability serial_trace;
  CP2OutputCapability callback_trace;
  CP2OutputCapability trajectory_trace;
  CP2OutputCapability updater_trace;
  CP2OutputCapability state_payload;
  CP2OutputCapability proposal_payload;
  CP2OutputCapability raw_system_payload;
  CP2OutputCapability timing_trace;
  CP2OutputCapability runtime_parameters;
  CP2OutputCapability loader_map_before;
  CP2OutputCapability loader_map_after;
  CP2OutputCapability legacy_state;
  CP2OutputCapability legacy_deviation;
  CP2OutputCapability legacy_timing;
};

/**
 * Writable child handle for an inode still held read-only by the runner.
 *
 * The handle also retains the canonical parent directory inode. It is movable
 * but not copyable and closes both descriptors on destruction.
 */
class CP2OpenedOutput final {
public:
  CP2OpenedOutput() noexcept = default;
  ~CP2OpenedOutput() noexcept;
  CP2OpenedOutput(const CP2OpenedOutput &) = delete;
  CP2OpenedOutput &operator=(const CP2OpenedOutput &) = delete;
  CP2OpenedOutput(CP2OpenedOutput &&other) noexcept;
  CP2OpenedOutput &operator=(CP2OpenedOutput &&other) noexcept;

  bool available() const noexcept {
    return descriptor_ >= 0 && parent_descriptor_ >= 0;
  }
  int descriptor() const noexcept { return descriptor_; }
  void Reset() noexcept;

private:
  friend bool OpenCP2OutputCapability(
      const std::string &, const CP2OutputCapability &,
      CP2OpenedOutput &) noexcept;
  friend bool SealCP2OutputCapability(
      CP2OpenedOutput &, std::uint64_t) noexcept;

  int descriptor_ = -1;
  int parent_descriptor_ = -1;
  std::string parent_path_;
  std::string leaf_;
  CP2OutputCapability capability_;
};

/** Parse exactly `null` or `v1:pid:fd:` followed by nine decimal stat fields. */
bool ParseCP2OutputCapability(const std::string &encoded,
                              CP2OutputCapability &output) noexcept;

/** Derive the only permitted kernel capability path. */
bool CP2OutputCapabilityPath(const CP2OutputCapability &capability,
                             std::string &output) noexcept;

/** Require the encoded inode to retain its exact private canonical name. */
bool ValidateCP2OutputCapabilityBinding(
    const std::string &canonical_path,
    const CP2OutputCapability &capability) noexcept;

/** Require the exact level-specific nonnull/null capability population. */
bool ValidateCP2RuntimeOutputCapabilities(
    const CP2RuntimeContext &context,
    const CP2RuntimeOutputCapabilities &capabilities) noexcept;

/** Open the held inode for writing after exact initial identity/name checks. */
bool OpenCP2OutputCapability(
    const std::string &canonical_path,
    const CP2OutputCapability &capability,
    CP2OpenedOutput &output) noexcept;

/** Seal one written capability as exact-size 0444 and fsync file and parent. */
bool SealCP2OutputCapability(CP2OpenedOutput &output,
                             std::uint64_t expected_size) noexcept;

/** Open, write all bytes once, seal, and close one held capability. */
bool WriteCP2OutputCapability(
    const std::string &canonical_path,
    const CP2OutputCapability &capability,
    const std::string &bytes) noexcept;

/**
 * Open an already held `/proc/PID/fd/FD` legacy sink without unlink or
 * truncation.  This is an explicit CP2-only bridge for the historical
 * std::ofstream writers; callers must first validate the corresponding
 * CP2OutputCapability and must not use this as a generic pathname fallback.
 */
bool OpenCP2PreopenedLegacyStream(
    const std::string &canonical_path,
    const std::string &capability_path,
    const CP2OutputCapability &capability,
    std::ofstream &output) noexcept;

} // namespace ov_msckf

#endif // OV_MSCKF_CP2_OUTPUT_CAPABILITY_H
