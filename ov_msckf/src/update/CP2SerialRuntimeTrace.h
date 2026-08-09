/*
 * SchurVIO-Lite CP2 value-only serial runtime trace.
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#ifndef OV_MSCKF_CP2_SERIAL_RUNTIME_TRACE_H
#define OV_MSCKF_CP2_SERIAL_RUNTIME_TRACE_H

#include "CP2RuntimeContext.h"
#include "CP2OutputCapability.h"
#include "CP2SerialPairing.h"
#include "UpdaterMSCKF.h"

#include <array>
#include <cstdint>
#include <string>
#include <vector>

namespace ov_msckf {

enum class CP2SerialRuntimeEnqueueStatus : std::uint8_t {
  kQueued,
  kFrequencyDropped,
  kCam0DecodeFailed,
  kCam1DecodeFailed,
  kProcessTerminated,
  kTraceFailure,
};

/**
 * One-run, value-only recorder for the serial/callback/trajectory joins.
 *
 * The recorder owns no ROS messages or estimator pointers.  It accepts the
 * independently selected pair population once, then enforces one enqueue and
 * at most one processing result per pair.  Updater identities must be the
 * exact contiguous call-entry sequence and must join the active pair by
 * camera-header timestamp.  Any violation is sticky and prevents output.
 */
class CP2SerialRuntimeTrace {
public:
  CP2SerialRuntimeTrace(CP2RuntimeContext context,
                        std::vector<CP2SerialPair> pairs) noexcept;

  /** Formal constructor: every emitted sink is a parent-held inode. */
  CP2SerialRuntimeTrace(
      CP2RuntimeContext context,
      CP2RuntimeOutputCapabilities output_capabilities,
      std::vector<CP2SerialPair> pairs) noexcept;

  bool NoteEnqueue(std::uint64_t pair_index,
                   CP2SerialRuntimeEnqueueStatus status) noexcept;

  bool NoteProcessing(
      const CP2UpdateInvocationContext &invocation_context,
      bool processing_entered,
      bool processing_returned, bool updater_invoked,
      bool state_row_emitted,
      const std::array<double, 3U> &position_G,
      const std::array<double, 4U> &quaternion_ItoG_xyzw,
      bool trace_fatal) noexcept;

  bool NoteUpdate(const CP2LiveUpdateEvent &event) noexcept;

  /**
   * Serialize every level-authorized sink exactly once.
   *
   * The formal constructor writes only through runner-held capabilities. The
   * two-argument constructor retains the older O_EXCL path solely for
   * artifact-free unit fixtures.
   */
  bool Finalize() noexcept;

  bool ready() const noexcept { return !failed_; }
  const std::string &failure() const noexcept { return failure_; }
  std::uint64_t pair_count() const noexcept {
    return static_cast<std::uint64_t>(rows_.size());
  }

  /** Read /proc/self/maps into a bounded owning byte string. */
  static bool ReadLoaderMap(std::string &output) noexcept;

  /**
   * Data-free fixture helper: create a single-link file and fsync it.
   * Formal CP2 execution must use WriteCP2OutputCapability instead.
   */
  static bool WriteNewFile(const std::string &path,
                           const std::string &bytes) noexcept;

private:
  struct UpdateRow {
    std::uint64_t invocation_id = 0U;
    std::uint64_t timing_start_ns = 0U;
    std::uint64_t timing_end_ns = 0U;
    std::uint64_t duration_ns = 0U;
    std::uint64_t input_feature_count = 0U;
    std::uint64_t raw_system_count = 0U;
    std::string terminal_status;
    std::string terminal_subreason;
    bool nonempty = false;
    bool baseline_preflight_attempted = false;
    bool preflight_accepted = false;
    bool committed = false;
    bool primary = false;
  };

  struct Row {
    CP2SerialPair pair;
    bool enqueue_noted = false;
    bool enqueue_entered = false;
    bool enqueue_returned = false;
    std::string enqueue_status = "not_entered";
    bool processing_noted = false;
    bool processing_entered = false;
    bool processing_returned = false;
    std::string processing_status = "not_queued";
    bool updater_invoked = false;
    std::vector<std::uint64_t> updater_invocation_ids;
    std::vector<UpdateRow> updates;
    bool state_row_emitted = false;
    bool trajectory_index_available = false;
    std::uint64_t trajectory_index = 0U;
    std::array<double, 3U> position_G{{0.0, 0.0, 0.0}};
    std::array<double, 4U> quaternion_ItoG_xyzw{{0.0, 0.0, 0.0, 1.0}};
  };

  void Reject(const char *message) noexcept;
  bool ValidateInitialPopulation() noexcept;
  bool SerializeSerial(std::string &output) const;
  bool SerializeCallbacks(std::string &output) const;
  bool SerializeTrajectory(std::string &output) const;
  bool SerializeUpdater(std::string &output) const;
  bool SerializeTiming(std::string &output) const;
  bool WriteOutput(const CP2NullablePath &path,
                   const CP2OutputCapability &capability,
                   const std::string &bytes) noexcept;

  CP2RuntimeContext context_;
  CP2RuntimeOutputCapabilities output_capabilities_;
  std::vector<Row> rows_;
  std::uint64_t next_invocation_id_ = 0U;
  std::uint64_t next_trajectory_index_ = 0U;
  std::string failure_;
  bool failed_ = false;
  bool finalized_ = false;
  bool held_capability_mode_ = false;
};

const char *cp2_serial_runtime_enqueue_status_name(
    CP2SerialRuntimeEnqueueStatus status) noexcept;

} // namespace ov_msckf

#endif // OV_MSCKF_CP2_SERIAL_RUNTIME_TRACE_H
