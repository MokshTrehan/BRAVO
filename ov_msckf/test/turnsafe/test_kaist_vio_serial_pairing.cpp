/*
 * TurnSafe KAIST-VIO target-time serial pairing tests.
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include "utils/KaistVioSerialPairing.h"

#include <gtest/gtest.h>

#include <cstdint>
#include <vector>

namespace {

using ov_msckf::KaistVioSerialMessage;
using ov_msckf::KaistVioSerialMessageKind;
using ov_msckf::KaistVioSerialPairingStatus;
using ov_msckf::KaistVioSerialPairSelector;

KaistVioSerialMessage Message(KaistVioSerialMessageKind kind,
                              std::uint64_t record_time_ns,
                              std::uint64_t header_time_ns = 0U) {
  KaistVioSerialMessage message;
  message.kind = kind;
  message.record_time_ns = record_time_ns;
  message.header_time_ns = header_time_ns;
  return message;
}

} // namespace

TEST(KaistVioSerialPairing, JoinsExactHeadersAcrossLargeRecordSkew) {
  const std::vector<KaistVioSerialMessage> messages = {
      Message(KaistVioSerialMessageKind::kCamera0, UINT64_C(100000000),
              UINT64_C(1000000000)),
      Message(KaistVioSerialMessageKind::kImu, UINT64_C(110000000)),
      Message(KaistVioSerialMessageKind::kCamera1, UINT64_C(153519649),
              UINT64_C(1000000000)),
  };

  const auto result = KaistVioSerialPairSelector::Select(messages);
  ASSERT_TRUE(result.accepted());
  ASSERT_EQ(result.pairs.size(), 1U);
  EXPECT_EQ(result.pairs[0].anchor_filtered_index, 0U);
  EXPECT_EQ(result.pairs[0].cam0_filtered_index, 0U);
  EXPECT_EQ(result.pairs[0].cam1_filtered_index, 2U);
  EXPECT_EQ(result.pairs[0].camera_timestamp_ns, UINT64_C(1000000000));
  EXPECT_EQ(result.pairs[0].absolute_record_delta_ns, UINT64_C(53519649));
  EXPECT_EQ(result.record_delta_at_or_above_20ms, 1U);
  EXPECT_EQ(result.maximum_record_delta_ns, UINT64_C(53519649));
}

TEST(KaistVioSerialPairing, PreservesAndReportsUnmatchedCameraMessages) {
  const std::vector<KaistVioSerialMessage> messages = {
      Message(KaistVioSerialMessageKind::kCamera0, 10U, 100U),
      Message(KaistVioSerialMessageKind::kCamera1, 11U, 100U),
      Message(KaistVioSerialMessageKind::kCamera0, 20U, 200U),
      Message(KaistVioSerialMessageKind::kCamera1, 21U, 300U),
  };

  const auto result = KaistVioSerialPairSelector::Select(messages);
  ASSERT_TRUE(result.accepted());
  ASSERT_EQ(result.pairs.size(), 1U);
  EXPECT_EQ(result.camera0_count, 2U);
  EXPECT_EQ(result.camera1_count, 2U);
  EXPECT_EQ(result.camera0_without_match, 1U);
  EXPECT_EQ(result.camera1_without_match, 1U);
}

TEST(KaistVioSerialPairing, DispatchesAtEarlierOrdinalInIncreasingHeaderOrder) {
  const std::vector<KaistVioSerialMessage> messages = {
      Message(KaistVioSerialMessageKind::kCamera1, 10U, 100U),
      Message(KaistVioSerialMessageKind::kCamera0, 11U, 100U),
      Message(KaistVioSerialMessageKind::kCamera0, 20U, 200U),
      Message(KaistVioSerialMessageKind::kImu, 21U),
      Message(KaistVioSerialMessageKind::kCamera1, 22U, 200U),
  };

  const auto result = KaistVioSerialPairSelector::Select(messages);
  ASSERT_TRUE(result.accepted());
  ASSERT_EQ(result.pairs.size(), 2U);
  EXPECT_EQ(result.pairs[0].anchor_filtered_index, 0U);
  EXPECT_EQ(result.pairs[0].camera_timestamp_ns, 100U);
  EXPECT_EQ(result.pairs[1].anchor_filtered_index, 2U);
  EXPECT_EQ(result.pairs[1].camera_timestamp_ns, 200U);
}

TEST(KaistVioSerialPairing, DoesNotPairNearbyButUnequalHeaders) {
  const std::vector<KaistVioSerialMessage> messages = {
      Message(KaistVioSerialMessageKind::kCamera0, 10U, UINT64_C(1000000000)),
      Message(KaistVioSerialMessageKind::kCamera1, 11U, UINT64_C(1000000001)),
  };

  const auto result = KaistVioSerialPairSelector::Select(messages);
  EXPECT_EQ(result.status, KaistVioSerialPairingStatus::kNoExactPairs);
  EXPECT_TRUE(result.pairs.empty());
}

TEST(KaistVioSerialPairing, DuplicateHeaderFailsClosed) {
  const std::vector<KaistVioSerialMessage> messages = {
      Message(KaistVioSerialMessageKind::kCamera0, 10U, 100U),
      Message(KaistVioSerialMessageKind::kCamera0, 11U, 100U),
      Message(KaistVioSerialMessageKind::kCamera1, 12U, 100U),
  };

  const auto result = KaistVioSerialPairSelector::Select(messages);
  EXPECT_EQ(result.status,
            KaistVioSerialPairingStatus::kDuplicateCameraHeader);
  EXPECT_STREQ(ov_msckf::kaist_vio_serial_pairing_status_name(result.status),
               "duplicate_camera_header");
  EXPECT_TRUE(result.pairs.empty());
}

TEST(KaistVioSerialPairing, ReversedRecordOrDispatchHeaderFailsClosed) {
  const std::vector<KaistVioSerialMessage> reversed_record = {
      Message(KaistVioSerialMessageKind::kCamera0, 11U, 100U),
      Message(KaistVioSerialMessageKind::kCamera1, 10U, 100U),
  };
  EXPECT_EQ(KaistVioSerialPairSelector::Select(reversed_record).status,
            KaistVioSerialPairingStatus::kRecordTimeReversed);

  const std::vector<KaistVioSerialMessage> reversed_header = {
      Message(KaistVioSerialMessageKind::kCamera0, 10U, 200U),
      Message(KaistVioSerialMessageKind::kCamera1, 11U, 200U),
      Message(KaistVioSerialMessageKind::kCamera0, 20U, 100U),
      Message(KaistVioSerialMessageKind::kCamera1, 21U, 100U),
  };
  EXPECT_EQ(KaistVioSerialPairSelector::Select(reversed_header).status,
            KaistVioSerialPairingStatus::kCameraHeaderOrderReversed);

  const std::vector<KaistVioSerialMessage> unmatched_reversed_header = {
      Message(KaistVioSerialMessageKind::kCamera0, 10U, 200U),
      Message(KaistVioSerialMessageKind::kCamera0, 11U, 100U),
      Message(KaistVioSerialMessageKind::kCamera1, 12U, 200U),
  };
  EXPECT_EQ(
      KaistVioSerialPairSelector::Select(unmatched_reversed_header).status,
      KaistVioSerialPairingStatus::kCameraHeaderOrderReversed);
}

TEST(KaistVioSerialPairing, InvalidKindFailsAtomically) {
  const std::vector<KaistVioSerialMessage> messages = {
      Message(static_cast<KaistVioSerialMessageKind>(255U), 10U),
  };
  const auto result = KaistVioSerialPairSelector::Select(messages);
  EXPECT_EQ(result.status, KaistVioSerialPairingStatus::kInvalidMessageKind);
  EXPECT_TRUE(result.pairs.empty());
}

TEST(KaistVioSerialPairing, ZeroCameraHeaderFailsAtomically) {
  const std::vector<KaistVioSerialMessage> messages = {
      Message(KaistVioSerialMessageKind::kCamera0, 10U, 0U),
      Message(KaistVioSerialMessageKind::kCamera1, 11U, 0U),
  };
  const auto result = KaistVioSerialPairSelector::Select(messages);
  EXPECT_EQ(result.status, KaistVioSerialPairingStatus::kZeroCameraHeader);
  EXPECT_TRUE(result.pairs.empty());
}
