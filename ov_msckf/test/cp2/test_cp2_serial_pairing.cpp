/*
 * SchurVIO-Lite CP2 deterministic serial-pair selection tests.
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include "update/CP2SerialPairing.h"

#include <gtest/gtest.h>

#include <cstdint>
#include <limits>
#include <vector>

namespace {

using ov_msckf::CP2SerialFilteredMessage;
using ov_msckf::CP2SerialMessageKind;
using ov_msckf::CP2SerialPairSelector;
using ov_msckf::CP2SerialPairingStatus;

CP2SerialFilteredMessage Message(CP2SerialMessageKind kind,
                                 std::uint64_t record_time_ns,
                                 std::uint64_t header_time_ns = 0U) {
  CP2SerialFilteredMessage message;
  message.kind = kind;
  message.record_time_ns = record_time_ns;
  message.header_time_ns = header_time_ns;
  return message;
}

} // namespace

TEST(CP2SerialPairing, StrictTwentyMillisecondBoundaryAndMetadataAreExact) {
  const std::vector<CP2SerialFilteredMessage> messages = {
      Message(CP2SerialMessageKind::kCamera0, UINT64_C(100000000),
              UINT64_C(1000000000)),
      Message(CP2SerialMessageKind::kImu, UINT64_C(110000000)),
      Message(CP2SerialMessageKind::kCamera1, UINT64_C(119999999),
              UINT64_C(1000000007)),
      Message(CP2SerialMessageKind::kCamera0, UINT64_C(200000000),
              UINT64_C(2000000000)),
      Message(CP2SerialMessageKind::kCamera1, UINT64_C(220000000),
              UINT64_C(2000000008)),
      Message(CP2SerialMessageKind::kCamera1, UINT64_C(300000000),
              UINT64_C(3000000009)),
      Message(CP2SerialMessageKind::kCamera0, UINT64_C(319999999),
              UINT64_C(3000000000)),
  };

  const auto result = CP2SerialPairSelector::Select(2U, messages);
  ASSERT_EQ(result.status, CP2SerialPairingStatus::kAccepted);
  ASSERT_EQ(result.pairs.size(), 2U);

  const auto &first = result.pairs[0];
  EXPECT_EQ(first.sequence_index, 2U);
  EXPECT_EQ(first.pair_index, 0U);
  EXPECT_EQ(first.anchor_filtered_index, 0U);
  EXPECT_EQ(first.anchor_camera_id, 0U);
  EXPECT_EQ(first.cam0_filtered_index, 0U);
  EXPECT_EQ(first.cam1_filtered_index, 2U);
  EXPECT_EQ(first.cam0_record_time_ns, UINT64_C(100000000));
  EXPECT_EQ(first.cam1_record_time_ns, UINT64_C(119999999));
  EXPECT_EQ(first.cam0_header_time_ns, UINT64_C(1000000000));
  EXPECT_EQ(first.cam1_header_time_ns, UINT64_C(1000000007));
  EXPECT_EQ(first.camera_timestamp_ns, first.cam0_header_time_ns);
  EXPECT_EQ(first.absolute_record_delta_ns, UINT64_C(19999999));

  // The exact 20,000,000 ns pair at indices 3/4 is rejected. The later
  // cam1 anchor therefore pairs with cam0 and is normalized without losing
  // its actual anchor identity or the strict forward record-time difference.
  const auto &second = result.pairs[1];
  EXPECT_EQ(second.pair_index, 1U);
  EXPECT_EQ(second.anchor_filtered_index, 5U);
  EXPECT_EQ(second.anchor_camera_id, 1U);
  EXPECT_EQ(second.cam0_filtered_index, 6U);
  EXPECT_EQ(second.cam1_filtered_index, 5U);
  EXPECT_EQ(second.absolute_record_delta_ns, UINT64_C(19999999));
  EXPECT_EQ(second.camera_timestamp_ns, UINT64_C(3000000000));
}

TEST(CP2SerialPairing, FirstForwardCandidateIsNeverReplacedByANearerMessage) {
  const std::vector<CP2SerialFilteredMessage> messages = {
      Message(CP2SerialMessageKind::kCamera0, UINT64_C(100000000),
              UINT64_C(1000000000)),
      Message(CP2SerialMessageKind::kImu, UINT64_C(100100000)),
      Message(CP2SerialMessageKind::kCamera1, UINT64_C(110000000),
              UINT64_C(2000000000)),
      Message(CP2SerialMessageKind::kCamera1, UINT64_C(110000001),
              UINT64_C(1000000001)),
  };

  const auto result = CP2SerialPairSelector::Select(0U, messages);
  ASSERT_TRUE(result.accepted());
  ASSERT_EQ(result.pairs.size(), 1U);
  // The later cam1 has a much nearer header timestamp, but headers never
  // choose the pair population and cannot replace the first forward ordinal.
  EXPECT_EQ(result.pairs[0].cam1_filtered_index, 2U);
  EXPECT_EQ(result.pairs[0].cam1_header_time_ns, UINT64_C(2000000000));
  EXPECT_EQ(result.pairs[0].absolute_record_delta_ns, UINT64_C(10000000));
}

TEST(CP2SerialPairing, UsedFirstForwardCandidateCannotBeReusedOrSearchedPast) {
  const std::vector<CP2SerialFilteredMessage> messages = {
      Message(CP2SerialMessageKind::kCamera0, UINT64_C(100000000)),
      Message(CP2SerialMessageKind::kCamera0, UINT64_C(101000000)),
      Message(CP2SerialMessageKind::kCamera1, UINT64_C(102000000)),
      Message(CP2SerialMessageKind::kCamera1, UINT64_C(103000000)),
  };

  const auto result = CP2SerialPairSelector::Select(1U, messages);
  ASSERT_TRUE(result.accepted());
  ASSERT_EQ(result.pairs.size(), 1U);
  EXPECT_EQ(result.pairs[0].anchor_filtered_index, 0U);
  EXPECT_EQ(result.pairs[0].cam1_filtered_index, 2U);
}

TEST(CP2SerialPairing, IntegerNanosecondCompositionChecksRangeAndOverflow) {
  std::uint64_t output = UINT64_C(123);
  EXPECT_TRUE(CP2SerialPairSelector::ComposeNanoseconds(
      0U, UINT64_C(999999999), output));
  EXPECT_EQ(output, UINT64_C(999999999));

  const std::uint64_t maximum = std::numeric_limits<std::uint64_t>::max();
  const std::uint64_t seconds =
      maximum / CP2SerialPairSelector::kNanosecondsPerSecond;
  const std::uint64_t remainder =
      maximum % CP2SerialPairSelector::kNanosecondsPerSecond;
  EXPECT_TRUE(CP2SerialPairSelector::ComposeNanoseconds(seconds, remainder,
                                                        output));
  EXPECT_EQ(output, maximum);

  output = UINT64_C(456);
  ASSERT_LT(remainder + 1U,
            CP2SerialPairSelector::kNanosecondsPerSecond);
  EXPECT_FALSE(CP2SerialPairSelector::ComposeNanoseconds(
      seconds, remainder + 1U, output));
  EXPECT_EQ(output, UINT64_C(456));
  EXPECT_FALSE(CP2SerialPairSelector::ComposeNanoseconds(
      0U, CP2SerialPairSelector::kNanosecondsPerSecond, output));
  EXPECT_EQ(output, UINT64_C(456));
}

TEST(CP2SerialPairing, InvalidMessageKindFailsAtomicallyWithFrozenStatus) {
  const std::vector<CP2SerialFilteredMessage> messages = {
      Message(CP2SerialMessageKind::kCamera0, 10U),
      Message(static_cast<CP2SerialMessageKind>(255U), 11U),
      Message(CP2SerialMessageKind::kCamera1, 12U),
  };

  const auto result = CP2SerialPairSelector::Select(0U, messages);
  EXPECT_EQ(result.status, CP2SerialPairingStatus::kInvalidMessageKind);
  EXPECT_STREQ(ov_msckf::cp2_serial_pairing_status_name(result.status),
               "invalid_message_kind");
  EXPECT_TRUE(result.pairs.empty());

  const std::vector<CP2SerialFilteredMessage> reversed_messages = {
      Message(CP2SerialMessageKind::kCamera0, 20U),
      Message(CP2SerialMessageKind::kImu, 19U),
      Message(CP2SerialMessageKind::kCamera1, 21U),
  };
  const auto reversed =
      CP2SerialPairSelector::Select(0U, reversed_messages);
  EXPECT_EQ(reversed.status, CP2SerialPairingStatus::kRecordTimeReversed);
  EXPECT_STREQ(ov_msckf::cp2_serial_pairing_status_name(reversed.status),
               "record_time_reversed");
  EXPECT_TRUE(reversed.pairs.empty());
}

TEST(CP2SerialPairing, EvidenceModeConsumesImuTailPastLastCamera) {
  EXPECT_TRUE(ov_msckf::cp2_serial_should_stop_iteration(true, false, false,
                                                        true));
  EXPECT_FALSE(ov_msckf::cp2_serial_should_stop_iteration(true, false, true,
                                                         true));
  EXPECT_TRUE(ov_msckf::cp2_serial_should_stop_iteration(true, true, true,
                                                        true));
  EXPECT_TRUE(ov_msckf::cp2_serial_should_stop_iteration(false, false, true,
                                                        true));
}
