/*
 * SchurVIO-Lite CP2 deterministic serial-pair selection.
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include "CP2SerialPairing.h"

#include <cstddef>
#include <limits>
#include <utility>

namespace {

static_assert(std::numeric_limits<std::size_t>::digits <=
                  std::numeric_limits<std::uint64_t>::digits,
              "CP2 filtered-message indices must be representable as u64");

bool cp2_serial_message_kind_valid(ov_msckf::CP2SerialMessageKind kind) noexcept {
  switch (kind) {
  case ov_msckf::CP2SerialMessageKind::kImu:
  case ov_msckf::CP2SerialMessageKind::kCamera0:
  case ov_msckf::CP2SerialMessageKind::kCamera1:
    return true;
  }
  return false;
}

bool cp2_serial_message_is_camera(ov_msckf::CP2SerialMessageKind kind) noexcept {
  return kind == ov_msckf::CP2SerialMessageKind::kCamera0 ||
         kind == ov_msckf::CP2SerialMessageKind::kCamera1;
}

std::uint64_t cp2_serial_camera_id(ov_msckf::CP2SerialMessageKind kind) noexcept {
  return kind == ov_msckf::CP2SerialMessageKind::kCamera0 ? 0U : 1U;
}

std::uint64_t cp2_absolute_u64_difference(std::uint64_t left,
                                          std::uint64_t right) noexcept {
  return left >= right ? left - right : right - left;
}

} // namespace

constexpr std::uint64_t
    ov_msckf::CP2SerialPairSelector::kStrictMaximumRecordDeltaNs;
constexpr std::uint64_t
    ov_msckf::CP2SerialPairSelector::kNanosecondsPerSecond;

const char *ov_msckf::cp2_serial_pairing_status_name(
    CP2SerialPairingStatus status) noexcept {
  switch (status) {
  case CP2SerialPairingStatus::kAccepted:
    return "accepted";
  case CP2SerialPairingStatus::kInvalidMessageKind:
    return "invalid_message_kind";
  case CP2SerialPairingStatus::kPairCountOverflow:
    return "pair_count_overflow";
  case CP2SerialPairingStatus::kAllocationFailure:
    return "allocation_failure";
  }
  return "invalid_status";
}

bool ov_msckf::cp2_serial_should_stop_iteration(
    bool ros_ok, bool after_time_finish, bool cp2_evidence_mode,
    bool after_max_camera_time) noexcept {
  return !ros_ok || after_time_finish ||
         (!cp2_evidence_mode && after_max_camera_time);
}

bool ov_msckf::CP2SerialPairSelector::ComposeNanoseconds(
    std::uint64_t seconds, std::uint64_t subsecond_nanoseconds,
    std::uint64_t &output) noexcept {
  if (subsecond_nanoseconds >= kNanosecondsPerSecond) {
    return false;
  }
  const std::uint64_t maximum = std::numeric_limits<std::uint64_t>::max();
  if (seconds >
      (maximum - subsecond_nanoseconds) / kNanosecondsPerSecond) {
    return false;
  }
  output = seconds * kNanosecondsPerSecond + subsecond_nanoseconds;
  return true;
}

ov_msckf::CP2SerialPairingResult ov_msckf::CP2SerialPairSelector::Select(
    std::uint64_t sequence_index,
    const std::vector<CP2SerialFilteredMessage> &messages) noexcept {
  CP2SerialPairingResult result;
  result.status = CP2SerialPairingStatus::kAccepted;

  for (const CP2SerialFilteredMessage &message : messages) {
    if (!cp2_serial_message_kind_valid(message.kind)) {
      result.status = CP2SerialPairingStatus::kInvalidMessageKind;
      return result;
    }
  }

  try {
    std::vector<std::uint8_t> used(messages.size(), UINT8_C(0));
    std::vector<CP2SerialPair> selected;
    selected.reserve(messages.size() / 2U);
    std::uint64_t next_pair_index = 0U;

    for (std::size_t anchor_index = 0U; anchor_index < messages.size();
         ++anchor_index) {
      const CP2SerialFilteredMessage &anchor = messages[anchor_index];
      if (!cp2_serial_message_is_camera(anchor.kind) ||
          used[anchor_index] != UINT8_C(0)) {
        continue;
      }

      const CP2SerialMessageKind other_kind =
          anchor.kind == CP2SerialMessageKind::kCamera0
              ? CP2SerialMessageKind::kCamera1
              : CP2SerialMessageKind::kCamera0;
      std::size_t candidate_index = messages.size();
      for (std::size_t index = anchor_index + 1U; index < messages.size();
           ++index) {
        if (messages[index].kind == other_kind) {
          candidate_index = index;
          break;
        }
      }
      if (candidate_index == messages.size() ||
          used[candidate_index] != UINT8_C(0)) {
        continue;
      }

      const CP2SerialFilteredMessage &candidate = messages[candidate_index];
      const std::uint64_t absolute_delta = cp2_absolute_u64_difference(
          anchor.record_time_ns, candidate.record_time_ns);
      if (absolute_delta >= kStrictMaximumRecordDeltaNs) {
        continue;
      }
      if (next_pair_index == std::numeric_limits<std::uint64_t>::max()) {
        result.status = CP2SerialPairingStatus::kPairCountOverflow;
        result.pairs.clear();
        return result;
      }

      const bool anchor_is_cam0 =
          anchor.kind == CP2SerialMessageKind::kCamera0;
      const std::size_t cam0_index =
          anchor_is_cam0 ? anchor_index : candidate_index;
      const std::size_t cam1_index =
          anchor_is_cam0 ? candidate_index : anchor_index;
      const CP2SerialFilteredMessage &cam0 = messages[cam0_index];
      const CP2SerialFilteredMessage &cam1 = messages[cam1_index];

      CP2SerialPair pair;
      pair.sequence_index = sequence_index;
      pair.pair_index = next_pair_index;
      pair.anchor_filtered_index = static_cast<std::uint64_t>(anchor_index);
      pair.anchor_camera_id = cp2_serial_camera_id(anchor.kind);
      pair.cam0_filtered_index = static_cast<std::uint64_t>(cam0_index);
      pair.cam1_filtered_index = static_cast<std::uint64_t>(cam1_index);
      pair.cam0_record_time_ns = cam0.record_time_ns;
      pair.cam1_record_time_ns = cam1.record_time_ns;
      pair.cam0_header_time_ns = cam0.header_time_ns;
      pair.cam1_header_time_ns = cam1.header_time_ns;
      pair.camera_timestamp_ns = cam0.header_time_ns;
      pair.absolute_record_delta_ns = absolute_delta;
      selected.push_back(std::move(pair));

      used[anchor_index] = UINT8_C(1);
      used[candidate_index] = UINT8_C(1);
      ++next_pair_index;
    }

    result.pairs = std::move(selected);
  } catch (...) {
    result.status = CP2SerialPairingStatus::kAllocationFailure;
    result.pairs.clear();
  }
  return result;
}
