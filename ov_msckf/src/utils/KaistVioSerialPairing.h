/*
 * TurnSafe KAIST-VIO target-time serial pairing.
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#ifndef OV_MSCKF_KAIST_VIO_SERIAL_PAIRING_H
#define OV_MSCKF_KAIST_VIO_SERIAL_PAIRING_H

#include <cstdint>
#include <vector>

namespace ov_msckf {

enum class KaistVioSerialMessageKind : std::uint8_t {
  kImu = 0,
  kCamera0 = 1,
  kCamera1 = 2,
};

/** Value-only metadata for one topic-filtered rosbag message. */
struct KaistVioSerialMessage {
  KaistVioSerialMessageKind kind = KaistVioSerialMessageKind::kImu;
  std::uint64_t record_time_ns = 0U;
  std::uint64_t header_time_ns = 0U;
};

/** One exact-header stereo join, dispatched at its earlier record ordinal. */
struct KaistVioSerialPair {
  std::uint64_t anchor_filtered_index = 0U;
  std::uint64_t cam0_filtered_index = 0U;
  std::uint64_t cam1_filtered_index = 0U;
  std::uint64_t camera_timestamp_ns = 0U;
  std::uint64_t absolute_record_delta_ns = 0U;
};

enum class KaistVioSerialPairingStatus : std::uint8_t {
  kAccepted,
  kInvalidMessageKind,
  kRecordTimeReversed,
  kZeroCameraHeader,
  kDuplicateCameraHeader,
  kCameraHeaderOrderReversed,
  kNoExactPairs,
  kIndexOverflow,
  kAllocationFailure,
};

const char *kaist_vio_serial_pairing_status_name(
    KaistVioSerialPairingStatus status) noexcept;

struct KaistVioSerialPairingResult {
  KaistVioSerialPairingStatus status =
      KaistVioSerialPairingStatus::kAllocationFailure;
  std::vector<KaistVioSerialPair> pairs;
  std::uint64_t camera0_count = 0U;
  std::uint64_t camera1_count = 0U;
  std::uint64_t camera0_without_match = 0U;
  std::uint64_t camera1_without_match = 0U;
  std::uint64_t record_delta_at_or_above_20ms = 0U;
  std::uint64_t maximum_record_delta_ns = 0U;

  bool accepted() const noexcept {
    return status == KaistVioSerialPairingStatus::kAccepted;
  }
};

/**
 * Dataset-specific, exact target-time join for the KAIST-VIO serial harness.
 *
 * Camera headers are joined only when their integer nanosecond stamps are
 * identical. Messages, record times, and header times are never rewritten.
 * An exact pair is anchored at the earlier of its two rosbag ordinals. The
 * serial reader already owns the complete immutable view, and early anchoring
 * ensures a later IMU can drain the final camera callback without synthesizing
 * data. Duplicate or non-increasing per-camera header stamps, reversed rosbag
 * record time, a zero camera header, non-increasing pair dispatch time, and a
 * bag with no exact pair fail closed. Unmatched
 * camera messages remain in the source/adapted bag and are counted explicitly;
 * they are not sent to a stereo-only callback.
 */
class KaistVioSerialPairSelector {
public:
  static constexpr std::uint64_t kRecordDeltaAuditThresholdNs =
      UINT64_C(20000000);

  static KaistVioSerialPairingResult
  Select(const std::vector<KaistVioSerialMessage> &messages) noexcept;
};

} // namespace ov_msckf

#endif // OV_MSCKF_KAIST_VIO_SERIAL_PAIRING_H
