/*
 * TurnSafe KAIST-VIO target-time serial pairing.
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include "KaistVioSerialPairing.h"

#include <algorithm>
#include <cstddef>
#include <limits>
#include <map>
#include <utility>

namespace {

using ov_msckf::KaistVioSerialMessage;
using ov_msckf::KaistVioSerialMessageKind;
using ov_msckf::KaistVioSerialPair;

bool kind_valid(KaistVioSerialMessageKind kind) noexcept {
  switch (kind) {
  case KaistVioSerialMessageKind::kImu:
  case KaistVioSerialMessageKind::kCamera0:
  case KaistVioSerialMessageKind::kCamera1:
    return true;
  }
  return false;
}

std::uint64_t absolute_difference(std::uint64_t left,
                                  std::uint64_t right) noexcept {
  return left >= right ? left - right : right - left;
}

bool increment(std::uint64_t &value) noexcept {
  if (value == std::numeric_limits<std::uint64_t>::max()) {
    return false;
  }
  ++value;
  return true;
}

} // namespace

constexpr std::uint64_t
    ov_msckf::KaistVioSerialPairSelector::kRecordDeltaAuditThresholdNs;

const char *ov_msckf::kaist_vio_serial_pairing_status_name(
    KaistVioSerialPairingStatus status) noexcept {
  switch (status) {
  case KaistVioSerialPairingStatus::kAccepted:
    return "accepted";
  case KaistVioSerialPairingStatus::kInvalidMessageKind:
    return "invalid_message_kind";
  case KaistVioSerialPairingStatus::kRecordTimeReversed:
    return "record_time_reversed";
  case KaistVioSerialPairingStatus::kZeroCameraHeader:
    return "zero_camera_header";
  case KaistVioSerialPairingStatus::kDuplicateCameraHeader:
    return "duplicate_camera_header";
  case KaistVioSerialPairingStatus::kCameraHeaderOrderReversed:
    return "camera_header_order_reversed";
  case KaistVioSerialPairingStatus::kNoExactPairs:
    return "no_exact_pairs";
  case KaistVioSerialPairingStatus::kIndexOverflow:
    return "index_overflow";
  case KaistVioSerialPairingStatus::kAllocationFailure:
    return "allocation_failure";
  }
  return "invalid_status";
}

ov_msckf::KaistVioSerialPairingResult
ov_msckf::KaistVioSerialPairSelector::Select(
    const std::vector<KaistVioSerialMessage> &messages) noexcept {
  KaistVioSerialPairingResult result;
  result.status = KaistVioSerialPairingStatus::kAccepted;

  bool have_previous_record = false;
  std::uint64_t previous_record = 0U;
  for (const KaistVioSerialMessage &message : messages) {
    if (!kind_valid(message.kind)) {
      result.status = KaistVioSerialPairingStatus::kInvalidMessageKind;
      return result;
    }
    if (have_previous_record && message.record_time_ns < previous_record) {
      result.status = KaistVioSerialPairingStatus::kRecordTimeReversed;
      return result;
    }
    previous_record = message.record_time_ns;
    have_previous_record = true;
  }

  static_assert(std::numeric_limits<std::size_t>::digits <=
                    std::numeric_limits<std::uint64_t>::digits,
                "KAIST filtered-message index must fit in u64");
  try {
    std::map<std::uint64_t, std::size_t> camera0_by_header;
    std::map<std::uint64_t, std::size_t> camera1_by_header;
    bool have_camera0_header = false;
    bool have_camera1_header = false;
    std::uint64_t previous_camera0_header = 0U;
    std::uint64_t previous_camera1_header = 0U;
    for (std::size_t index = 0U; index < messages.size(); ++index) {
      const KaistVioSerialMessage &message = messages[index];
      if (message.kind == KaistVioSerialMessageKind::kCamera0) {
        if (!increment(result.camera0_count)) {
          result.status = KaistVioSerialPairingStatus::kIndexOverflow;
          return result;
        }
        if (message.header_time_ns == 0U) {
          result.status = KaistVioSerialPairingStatus::kZeroCameraHeader;
          return result;
        }
        if (have_camera0_header &&
            message.header_time_ns == previous_camera0_header) {
          result.status =
              KaistVioSerialPairingStatus::kDuplicateCameraHeader;
          return result;
        }
        if (have_camera0_header &&
            message.header_time_ns < previous_camera0_header) {
          result.status =
              KaistVioSerialPairingStatus::kCameraHeaderOrderReversed;
          return result;
        }
        if (!camera0_by_header.emplace(message.header_time_ns, index).second) {
          result.status =
              KaistVioSerialPairingStatus::kDuplicateCameraHeader;
          return result;
        }
        previous_camera0_header = message.header_time_ns;
        have_camera0_header = true;
      } else if (message.kind == KaistVioSerialMessageKind::kCamera1) {
        if (!increment(result.camera1_count)) {
          result.status = KaistVioSerialPairingStatus::kIndexOverflow;
          return result;
        }
        if (message.header_time_ns == 0U) {
          result.status = KaistVioSerialPairingStatus::kZeroCameraHeader;
          return result;
        }
        if (have_camera1_header &&
            message.header_time_ns == previous_camera1_header) {
          result.status =
              KaistVioSerialPairingStatus::kDuplicateCameraHeader;
          return result;
        }
        if (have_camera1_header &&
            message.header_time_ns < previous_camera1_header) {
          result.status =
              KaistVioSerialPairingStatus::kCameraHeaderOrderReversed;
          return result;
        }
        if (!camera1_by_header.emplace(message.header_time_ns, index).second) {
          result.status =
              KaistVioSerialPairingStatus::kDuplicateCameraHeader;
          return result;
        }
        previous_camera1_header = message.header_time_ns;
        have_camera1_header = true;
      }
    }

    std::vector<KaistVioSerialPair> selected;
    selected.reserve(std::min(camera0_by_header.size(),
                              camera1_by_header.size()));
    for (const auto &camera0 : camera0_by_header) {
      const auto camera1 = camera1_by_header.find(camera0.first);
      if (camera1 == camera1_by_header.end()) {
        continue;
      }
      const std::size_t camera0_index = camera0.second;
      const std::size_t camera1_index = camera1->second;
      const std::size_t anchor_index =
          std::min(camera0_index, camera1_index);
      const std::uint64_t delta = absolute_difference(
          messages[camera0_index].record_time_ns,
          messages[camera1_index].record_time_ns);

      KaistVioSerialPair pair;
      pair.anchor_filtered_index =
          static_cast<std::uint64_t>(anchor_index);
      pair.cam0_filtered_index =
          static_cast<std::uint64_t>(camera0_index);
      pair.cam1_filtered_index =
          static_cast<std::uint64_t>(camera1_index);
      pair.camera_timestamp_ns = camera0.first;
      pair.absolute_record_delta_ns = delta;
      selected.push_back(std::move(pair));
      result.maximum_record_delta_ns =
          std::max(result.maximum_record_delta_ns, delta);
      if (delta >= kRecordDeltaAuditThresholdNs &&
          !increment(result.record_delta_at_or_above_20ms)) {
        result.status = KaistVioSerialPairingStatus::kIndexOverflow;
        return result;
      }
    }

    std::sort(selected.begin(), selected.end(),
              [](const KaistVioSerialPair &left,
                 const KaistVioSerialPair &right) {
                return left.anchor_filtered_index <
                       right.anchor_filtered_index;
              });
    if (selected.empty()) {
      result.status = KaistVioSerialPairingStatus::kNoExactPairs;
      return result;
    }
    for (std::size_t index = 1U; index < selected.size(); ++index) {
      if (selected[index].camera_timestamp_ns <=
          selected[index - 1U].camera_timestamp_ns) {
        result.status =
            KaistVioSerialPairingStatus::kCameraHeaderOrderReversed;
        return result;
      }
    }

    const std::uint64_t pair_count =
        static_cast<std::uint64_t>(selected.size());
    result.camera0_without_match = result.camera0_count - pair_count;
    result.camera1_without_match = result.camera1_count - pair_count;
    result.pairs = std::move(selected);
  } catch (...) {
    result = KaistVioSerialPairingResult{};
    result.status = KaistVioSerialPairingStatus::kAllocationFailure;
  }
  return result;
}
