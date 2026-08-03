/*
 * SchurVIO-Lite CP2 deterministic serial-pair selection.
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#ifndef OV_MSCKF_CP2_SERIAL_PAIRING_H
#define OV_MSCKF_CP2_SERIAL_PAIRING_H

#include <cstdint>
#include <vector>

namespace ov_msckf {

/** Exact kind of one message in the post-offset, topic-filtered serial view. */
enum class CP2SerialMessageKind : std::uint8_t {
  kImu = 0,
  kCamera0 = 1,
  kCamera1 = 2,
};

/**
 * Value-only metadata for one message in rosbag iteration order.
 *
 * The vector position is its filtered-message ordinal. Camera record and
 * header times are already exact nonnegative integer nanoseconds; no binary64
 * timestamp participates in pairing.
 */
struct CP2SerialFilteredMessage {
  CP2SerialMessageKind kind = CP2SerialMessageKind::kImu;
  std::uint64_t record_time_ns = 0U;
  std::uint64_t header_time_ns = 0U;
};

/** Complete normalized metadata for one selected stereo pair. */
struct CP2SerialPair {
  std::uint64_t sequence_index = 0U;
  std::uint64_t pair_index = 0U;
  std::uint64_t anchor_filtered_index = 0U;
  std::uint64_t anchor_camera_id = 0U;
  std::uint64_t cam0_filtered_index = 0U;
  std::uint64_t cam1_filtered_index = 0U;
  std::uint64_t cam0_record_time_ns = 0U;
  std::uint64_t cam1_record_time_ns = 0U;
  std::uint64_t cam0_header_time_ns = 0U;
  std::uint64_t cam1_header_time_ns = 0U;
  std::uint64_t camera_timestamp_ns = 0U;
  std::uint64_t absolute_record_delta_ns = 0U;
};

enum class CP2SerialPairingStatus : std::uint8_t {
  kAccepted,
  kInvalidMessageKind,
  kPairCountOverflow,
  kAllocationFailure,
};

const char *cp2_serial_pairing_status_name(CP2SerialPairingStatus status) noexcept;

/**
 * Frozen serial-view termination policy.
 *
 * Evidence runs must continue past the last camera record so a later IMU can
 * drain the final queued camera measurement; all runs still stop at the
 * configured view end or ROS shutdown.
 */
bool cp2_serial_should_stop_iteration(bool ros_ok, bool after_time_finish,
                                      bool cp2_evidence_mode,
                                      bool after_max_camera_time) noexcept;

struct CP2SerialPairingResult {
  CP2SerialPairingStatus status = CP2SerialPairingStatus::kAllocationFailure;
  std::vector<CP2SerialPair> pairs;

  bool accepted() const noexcept {
    return status == CP2SerialPairingStatus::kAccepted;
  }
};

/** Frozen first-forward, no-reuse stereo selector for CP2-C/D/E runners. */
class CP2SerialPairSelector {
public:
  static constexpr std::uint64_t kStrictMaximumRecordDeltaNs = UINT64_C(20000000);
  static constexpr std::uint64_t kNanosecondsPerSecond = UINT64_C(1000000000);

  /**
   * Form exact nonnegative nanoseconds without clobbering output on failure.
   * Subsecond nanoseconds must be strictly below one second.
   */
  static bool ComposeNanoseconds(std::uint64_t seconds,
                                 std::uint64_t subsecond_nanoseconds,
                                 std::uint64_t &output) noexcept;

  /**
   * Select stereo pairs from the complete topic-filtered view.
   *
   * Scan anchors in vector order. For an unused camera anchor, inspect only
   * the first later message from the other camera. Accept it iff it is unused
   * and its exact record-time difference is strictly below 20,000,000 ns.
   * Accepted camera messages are never reused. Output is failure-atomic.
   */
  static CP2SerialPairingResult
  Select(std::uint64_t sequence_index,
         const std::vector<CP2SerialFilteredMessage> &messages) noexcept;
};

} // namespace ov_msckf

#endif // OV_MSCKF_CP2_SERIAL_PAIRING_H
