/*
 * SchurVIO-Lite CP2 canonical trace codec and offline replay tests.
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include "update/CP2TraceCodec.h"
#include "update/CP2Canonical.h"

#include <gtest/gtest.h>

#include <Eigen/Core>

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <limits>
#include <stdexcept>
#include <string>
#include <type_traits>
#include <vector>

namespace {

std::uint64_t binary64_bits(double value) {
  std::uint64_t bits = 0U;
  std::memcpy(&bits, &value, sizeof(bits));
  return bits;
}

template <typename LeftDerived, typename RightDerived>
void expect_matrix_bits_equal(const Eigen::MatrixBase<LeftDerived> &left,
                              const Eigen::MatrixBase<RightDerived> &right) {
  ASSERT_EQ(left.rows(), right.rows());
  ASSERT_EQ(left.cols(), right.cols());
  for (Eigen::Index row = 0; row < left.rows(); ++row) {
    for (Eigen::Index column = 0; column < left.cols(); ++column) {
      EXPECT_EQ(binary64_bits(left.derived().coeff(row, column)),
                binary64_bits(right.derived().coeff(row, column)))
          << "row=" << row << " column=" << column;
    }
  }
}

void store_u64(std::vector<std::uint8_t> &bytes, std::size_t offset, std::uint64_t value) {
  ASSERT_LE(offset + 8U, bytes.size());
  for (std::size_t index = 0; index < 8U; ++index) {
    bytes[offset + index] = static_cast<std::uint8_t>(value >> (56U - 8U * index));
  }
}

std::vector<std::uint8_t> hex_bytes(const std::string &hex) {
  if (hex.size() % 2U != 0U) {
    throw std::invalid_argument("odd known-answer hex length");
  }
  const auto nibble = [](char value) -> std::uint8_t {
    if (value >= '0' && value <= '9') {
      return static_cast<std::uint8_t>(value - '0');
    }
    if (value >= 'a' && value <= 'f') {
      return static_cast<std::uint8_t>(10 + value - 'a');
    }
    throw std::invalid_argument("non-lowercase known-answer hex");
  };
  std::vector<std::uint8_t> bytes(hex.size() / 2U);
  for (std::size_t index = 0; index < bytes.size(); ++index) {
    bytes[index] = static_cast<std::uint8_t>((nibble(hex[2U * index]) << 4U) |
                                             nibble(hex[2U * index + 1U]));
  }
  return bytes;
}

std::string sha256_hex(const std::vector<std::uint8_t> &bytes) {
  ov_msckf::CP2Sha256 sha256;
  sha256.Update(bytes);
  return sha256.HexDigest();
}

std::string bytes_hex(const std::vector<std::uint8_t> &bytes) {
  static constexpr char digits[] = "0123456789abcdef";
  std::string result(bytes.size() * 2U, '0');
  for (std::size_t index = 0; index < bytes.size(); ++index) {
    result[2U * index] = digits[bytes[index] >> 4U];
    result[2U * index + 1U] = digits[bytes[index] & 0x0fU];
  }
  return result;
}

ov_msckf::CP2FeatureGate::ChiSquaredTable finite_table(double value) {
  ov_msckf::CP2FeatureGate::ChiSquaredTable table;
  for (int degrees_of_freedom = 1; degrees_of_freedom < 500; ++degrees_of_freedom) {
    table.emplace(degrees_of_freedom, value);
  }
  return table;
}

ov_msckf::CP2RawFeatureSystem raw_fixture(std::uint64_t feature_id) {
  ov_msckf::CP2RawFeatureSystem raw;
  raw.feature_id = feature_id;
  raw.H_x.resize(2, 2);
  raw.H_x << 1.0, -0.0, -2.0, 3.5;
  raw.H_f.resize(2, 3);
  raw.H_f << 0.5, 1.0, 0.0, -0.0, -1.0, 2.0;
  raw.residual.resize(2);
  raw.residual << -0.0, 4.25;
  raw.jacobian_layout = {{5, 1, 0}, {1, 1, 1}};
  return raw;
}

ov_msckf::CP2RawFeatureSystem diagonal_raw(std::uint64_t feature_id,
                                           const Eigen::MatrixXd &reduced_H,
                                           const Eigen::VectorXd &reduced_residual) {
  ov_msckf::CP2RawFeatureSystem raw;
  raw.feature_id = feature_id;
  raw.H_f = Eigen::MatrixXd::Zero(reduced_H.rows() + 3, 3);
  raw.H_f(0, 0) = 3.0;
  raw.H_f(1, 1) = 2.0;
  raw.H_f(2, 2) = 1.0;
  raw.H_x = Eigen::MatrixXd::Zero(reduced_H.rows() + 3, reduced_H.cols());
  raw.H_x.bottomRows(reduced_H.rows()) = reduced_H;
  raw.residual = Eigen::VectorXd::Zero(reduced_residual.rows() + 3);
  raw.residual.tail(reduced_residual.rows()) = reduced_residual;
  raw.jacobian_layout = {{0, reduced_H.cols(), 0}};
  return raw;
}

ov_msckf::CP2TraceReplayInput replay_fixture() {
  ov_msckf::CP2TraceReplayInput input;
  input.invocation = {1, 9, 4};
  input.prior.covariance = Eigen::Matrix2d::Identity();
  input.prior.state_blocks = {{0, 2, 0}};
  input.sigma_px = 1.0;
  input.sigma_px_sq = 1.0;
  input.chi2_multiplier = 1.0;
  input.chi_squared_table = finite_table(1.0e6);

  Eigen::MatrixXd first_H(3, 2);
  first_H << 0.25, -0.10, 0.05, 0.30, -0.12, 0.08;
  Eigen::Vector3d first_residual;
  first_residual << 0.20, -0.15, 0.07;
  ov_msckf::CP2RawSystemTraceFrame first;
  first.invocation = input.invocation;
  first.feature_ordinal = 0;
  first.raw_system = diagonal_raw(7, first_H, first_residual);
  first.raw_system.H_f.bottomRows(3) << 0.20, -0.05, 0.08, -0.10, 0.12,
      0.03, 0.04, -0.07, 0.15;
  input.raw_frames.push_back(first);

  Eigen::MatrixXd second_H(3, 2);
  second_H << -0.08, 0.15, 0.22, -0.05, 0.11, 0.09;
  Eigen::Vector3d second_residual;
  second_residual << -0.04, 0.12, -0.09;
  ov_msckf::CP2RawSystemTraceFrame second;
  second.invocation = input.invocation;
  second.feature_ordinal = 1;
  second.raw_system = diagonal_raw(3, second_H, second_residual);
  second.raw_system.H_f.bottomRows(3) << -0.06, 0.09, 0.02, 0.11, -0.04,
      0.07, -0.03, 0.05, -0.08;
  input.raw_frames.push_back(second);
  return input;
}

ov_msckf::CP2TraceReplayInput sealed_replay_fixture() {
  ov_msckf::CP2TraceReplayInput input = replay_fixture();
  const ov_msckf::CP2EncodedRawSystemFile raw_file =
      ov_msckf::CP2TraceCodec::EncodeRawSystemFile(input.raw_frames);
  input.raw_system_file_bytes = raw_file.bytes;
  input.raw_frames = ov_msckf::CP2TraceCodec::DecodeRawSystemFile(raw_file.bytes);

  ov_msckf::CP2ShadowMathInput math_input;
  math_input.prior = input.prior;
  math_input.sigma_px = input.sigma_px;
  math_input.sigma_px_sq = input.sigma_px_sq;
  math_input.chi2_multiplier = input.chi2_multiplier;
  math_input.chi_squared_table = input.chi_squared_table;
  for (const ov_msckf::CP2RawSystemTraceFrame &frame : input.raw_frames) {
    math_input.raw_systems.push_back(frame.raw_system);
  }
  const ov_msckf::CP2ShadowMathResult math =
      ov_msckf::CP2ShadowMath::Process(math_input);
  if (!math.input_valid || !math.nullspace.proposal_available ||
      !math.schur.proposal_available) {
    throw std::runtime_error("replay known-answer fixture did not produce both proposals");
  }

  ov_msckf::CP2ProposalTraceFrame baseline;
  baseline.invocation = input.invocation;
  baseline.role = ov_msckf::CP2ProposalTraceRole::kNullspaceBaseline;
  baseline.proposal = {math.nullspace.proposal.dx, math.nullspace.proposal.P_plus};
  ov_msckf::CP2ProposalTraceFrame candidate;
  candidate.invocation = input.invocation;
  candidate.role = ov_msckf::CP2ProposalTraceRole::kSchurCandidate;
  candidate.proposal = {math.schur.proposal.dx, math.schur.proposal.P_plus};
  const ov_msckf::CP2EncodedProposalFile proposal_file =
      ov_msckf::CP2TraceCodec::EncodeProposalFile({baseline, candidate});
  input.proposal_file_bytes = proposal_file.bytes;
  input.proposal_frames =
      ov_msckf::CP2TraceCodec::DecodeProposalFile(proposal_file.bytes);
  return input;
}

TEST(CP2TraceRawPayload, FrozenDomainRowMajorBitsAndLayoutRoundTripExactly) {
  const ov_msckf::CP2RawFeatureSystem raw = raw_fixture(UINT64_C(0x0102030405060708));
  const std::vector<std::uint8_t> payload =
      ov_msckf::CP2TraceCodec::EncodeRawSystemPayload(raw);

  const std::string domain("SchurVIO-CP2-raw-system-v1\0", 27U);
  ASSERT_GE(payload.size(), domain.size());
  EXPECT_TRUE(std::equal(domain.begin(), domain.end(), payload.begin()));
  EXPECT_EQ(payload.size(), 219U);
  // Domain + feature ID + H_x shape. The first H_x coefficient is 1.0 and
  // the second is exact negative zero in logical row-major order.
  EXPECT_EQ(payload[35U + 15U], 2U);
  EXPECT_EQ(payload[35U + 16U], 0x3fU);
  EXPECT_EQ(payload[35U + 17U], 0xf0U);
  EXPECT_EQ(payload[35U + 24U], 0x80U);
  EXPECT_EQ(payload[35U + 31U], 0x00U);
  ov_msckf::CP2Sha256 payload_sha256;
  payload_sha256.Update(payload);
  EXPECT_EQ(payload_sha256.HexDigest(),
            "3fe0b5a24312f8a6d1b205c487c0a75e63a2fa916a264fa898fcb1c10aa5d2f8");

  const ov_msckf::CP2RawFeatureSystem decoded =
      ov_msckf::CP2TraceCodec::DecodeRawSystemPayload(payload);
  EXPECT_EQ(decoded.feature_id, raw.feature_id);
  expect_matrix_bits_equal(decoded.H_x, raw.H_x);
  expect_matrix_bits_equal(decoded.H_f, raw.H_f);
  expect_matrix_bits_equal(decoded.residual, raw.residual);
  ASSERT_EQ(decoded.jacobian_layout.size(), 2U);
  EXPECT_EQ(decoded.jacobian_layout[0].covariance_id, 5);
  EXPECT_EQ(decoded.jacobian_layout[0].size, 1);
  EXPECT_EQ(decoded.jacobian_layout[0].H_offset, 0);
  EXPECT_EQ(decoded.jacobian_layout[1].covariance_id, 1);
  EXPECT_EQ(decoded.jacobian_layout[1].size, 1);
  EXPECT_EQ(decoded.jacobian_layout[1].H_offset, 1);
  EXPECT_EQ(ov_msckf::CP2TraceCodec::EncodeRawSystemPayload(decoded), payload);
}

TEST(CP2TraceRawPayload, CorruptionAndAllocationBoundsFailClosed) {
  const std::vector<std::uint8_t> valid =
      ov_msckf::CP2TraceCodec::EncodeRawSystemPayload(raw_fixture(8));

  std::vector<std::uint8_t> bad_domain = valid;
  bad_domain[0] ^= 1U;
  EXPECT_THROW(ov_msckf::CP2TraceCodec::DecodeRawSystemPayload(bad_domain),
               ov_msckf::CP2TraceCodecError);

  std::vector<std::uint8_t> trailing = valid;
  trailing.push_back(0U);
  EXPECT_THROW(ov_msckf::CP2TraceCodec::DecodeRawSystemPayload(trailing),
               ov_msckf::CP2TraceCodecError);

  std::vector<std::uint8_t> truncated = valid;
  truncated.pop_back();
  EXPECT_THROW(ov_msckf::CP2TraceCodec::DecodeRawSystemPayload(truncated),
               ov_msckf::CP2TraceCodecError);

  // The layout count is the final list header at byte 179 for this fixture.
  // A hostile count must be rejected from the 0 remaining entry bytes before
  // reserve(), not after allocating a million layout objects.
  std::vector<std::uint8_t> high_count_without_entries(valid.begin(),
                                                        valid.begin() + 187);
  store_u64(high_count_without_entries, 179U, UINT64_C(1000000));
  EXPECT_THROW(
      ov_msckf::CP2TraceCodec::DecodeRawSystemPayload(high_count_without_entries),
      ov_msckf::CP2TraceCodecError);

  std::vector<std::uint8_t> impossible_rows = valid;
  store_u64(impossible_rows, 27U + 8U, std::numeric_limits<std::uint64_t>::max());
  EXPECT_THROW(ov_msckf::CP2TraceCodec::DecodeRawSystemPayload(impossible_rows),
               ov_msckf::CP2TraceCodecError);

  // This byte string ends immediately after the 2x2 H_x dimension header. A
  // three-coefficient aggregate budget must win before coefficient-byte
  // checking or Eigen allocation; a later check would report truncation.
  std::vector<std::uint8_t> dimensions_only(valid.begin(), valid.begin() + 51);
  ov_msckf::CP2TraceDecodeLimits preallocation_budget;
  preallocation_budget.maximum_total_matrix_coefficients = 3U;
  try {
    (void)ov_msckf::CP2TraceCodec::DecodeRawSystemPayload(
        dimensions_only, preallocation_budget);
    FAIL() << "aggregate dimension-header budget was not enforced";
  } catch (const ov_msckf::CP2TraceCodecError &error) {
    EXPECT_STREQ(error.what(),
                 "raw H_x exceeds configured aggregate coefficient limit");
  }

  ov_msckf::CP2TraceDecodeLimits small;
  small.maximum_matrix_coefficients = 3U;
  EXPECT_THROW(ov_msckf::CP2TraceCodec::DecodeRawSystemPayload(valid, small),
               ov_msckf::CP2TraceCodecError);

  ov_msckf::CP2RawFeatureSystem hidden_offset = raw_fixture(8);
  hidden_offset.jacobian_layout[1].H_offset = 0;
  EXPECT_THROW(ov_msckf::CP2TraceCodec::EncodeRawSystemPayload(hidden_offset),
               ov_msckf::CP2TraceCodecError);

  ov_msckf::CP2RawFeatureSystem overlapping = raw_fixture(8);
  overlapping.jacobian_layout = {{5, 1, 0}, {5, 1, 1}};
  EXPECT_THROW(ov_msckf::CP2TraceCodec::EncodeRawSystemPayload(overlapping),
               ov_msckf::CP2TraceCodecError);
}

TEST(CP2TraceAcceptedDigests, FrozenSetAndSequenceDomainsAreIndependent) {
  const ov_msckf::CP2AcceptedFeatureDigests digests =
      ov_msckf::CP2TraceCodec::AcceptedFeatureDigests({7, 3});
  EXPECT_EQ(digests.processing_sequence, (std::vector<std::uint64_t>{7, 3}));
  EXPECT_EQ(digests.sorted_set, (std::vector<std::uint64_t>{3, 7}));
  EXPECT_EQ(digests.set_sha256,
            "7c6f475259b73d0c3bb7b1ed2c885cc68d8f7c55e9a15409ca3f0466d4ac947a");
  EXPECT_EQ(digests.sequence_sha256,
            "309b2e00063377ddcffb0580b9f4f03f8a69ba84aaa388b09194aa4016792c0e");

  const ov_msckf::CP2AcceptedFeatureDigests reversed =
      ov_msckf::CP2TraceCodec::AcceptedFeatureDigests({3, 7});
  EXPECT_EQ(reversed.set_sha256, digests.set_sha256);
  EXPECT_NE(reversed.sequence_sha256, digests.sequence_sha256);

  const ov_msckf::CP2AcceptedFeatureDigests empty =
      ov_msckf::CP2TraceCodec::AcceptedFeatureDigests({});
  EXPECT_EQ(empty.set_sha256,
            "6a86312b1b83c51bc4e5bbf1e4174dc58f7c8012fd3757f9c65c76b3be0c4fe7");
  EXPECT_EQ(empty.sequence_sha256,
            "0b4fa91e745c11c085c17f843d5b0e1752adf2d154231face80a9591de23c3b8");
  EXPECT_THROW(ov_msckf::CP2TraceCodec::AcceptedFeatureDigests({7, 7}),
               ov_msckf::CP2TraceCodecError);
}

TEST(CP2TraceRawFile, CompleteFrameAndHeaderMatchIndependentKnownAnswer) {
  ov_msckf::CP2RawSystemTraceFrame frame;
  frame.invocation = {1, 2, 3};
  frame.feature_ordinal = 0;
  frame.raw_system = raw_fixture(UINT64_C(0x0102030405060708));
  const ov_msckf::CP2EncodedRawSystemFile encoded =
      ov_msckf::CP2TraceCodec::EncodeRawSystemFile({frame});

  const std::vector<std::uint8_t> expected = hex_bytes(
      "536368757256494f2d4350322d7261772d66696c652d76310a0000000000000000000000000000010000000000000002"
      "00000000000000030000000000000000010203040506070800000000000000db536368757256494f2d4350322d726177"
      "2d73797374656d2d7631000102030405060708000000000000000200000000000000023ff00000000000008000000000"
      "000000c000000000000000400c000000000000000000000000000200000000000000033fe00000000000003ff0000000"
      "00000000000000000000008000000000000000bff0000000000000400000000000000000000000000000020000000000"
      "000001800000000000000040110000000000000000000000000002000000000000000500000000000000010000000000"
      "0000010000000000000001");
  const std::vector<std::uint8_t> expected_header = hex_bytes(
      "0000000000000001000000000000000200000000000000030000000000000000"
      "010203040506070800000000000000db");
  ASSERT_EQ(encoded.bytes.size(), expected.size());
  EXPECT_EQ(encoded.bytes, expected);
  ASSERT_GE(encoded.bytes.size(), 32U + expected_header.size());
  EXPECT_TRUE(std::equal(expected_header.begin(), expected_header.end(),
                         encoded.bytes.begin() + 32));
  EXPECT_EQ(sha256_hex(encoded.bytes),
            "d873fe3d1b1eea5b8439dec140409d2ea137d2217e7becdcca42bef460037340");
  ASSERT_EQ(encoded.payloads.size(), 1U);
  EXPECT_EQ(encoded.payloads[0].offset, 80U);
  EXPECT_EQ(encoded.payloads[0].length, 219U);
}

TEST(CP2TraceRawFile, FrameContextOffsetsOrderingAndFeatureIdentityAreExact) {
  ov_msckf::CP2RawSystemTraceFrame first;
  first.invocation = {0, 2, 3};
  first.feature_ordinal = 0;
  first.raw_system = raw_fixture(7);
  ov_msckf::CP2RawSystemTraceFrame second = first;
  second.feature_ordinal = 1;
  second.raw_system.feature_id = 3;

  const ov_msckf::CP2EncodedRawSystemFile encoded =
      ov_msckf::CP2TraceCodec::EncodeRawSystemFile({first, second});
  ASSERT_EQ(encoded.payloads.size(), 2U);
  EXPECT_EQ(encoded.payloads[0].offset, 80U);
  EXPECT_EQ(encoded.payloads[0].length, 219U);
  EXPECT_EQ(encoded.payloads[1].offset, 80U + 219U + 48U);
  const std::string magic("SchurVIO-CP2-raw-file-v1\n\0\0\0\0\0\0\0", 32U);
  ASSERT_GE(encoded.bytes.size(), magic.size());
  EXPECT_TRUE(std::equal(magic.begin(), magic.end(), encoded.bytes.begin()));

  const std::vector<ov_msckf::CP2RawSystemTraceFrame> decoded =
      ov_msckf::CP2TraceCodec::DecodeRawSystemFile(encoded.bytes);
  ASSERT_EQ(decoded.size(), 2U);
  EXPECT_EQ(decoded[0].invocation, first.invocation);
  EXPECT_EQ(decoded[0].feature_ordinal, 0U);
  EXPECT_EQ(decoded[0].raw_system.feature_id, 7U);
  EXPECT_EQ(decoded[0].payload.offset, encoded.payloads[0].offset);
  EXPECT_EQ(decoded[0].payload.sha256, encoded.payloads[0].sha256);
  EXPECT_EQ(decoded[1].feature_ordinal, 1U);
  EXPECT_EQ(decoded[1].raw_system.feature_id, 3U);

  std::vector<std::uint8_t> disconnected = encoded.bytes;
  store_u64(disconnected, 32U + 4U * 8U, 99U);
  EXPECT_THROW(ov_msckf::CP2TraceCodec::DecodeRawSystemFile(disconnected),
               ov_msckf::CP2TraceCodecError);

  second.feature_ordinal = 2;
  EXPECT_THROW(ov_msckf::CP2TraceCodec::EncodeRawSystemFile({first, second}),
               ov_msckf::CP2TraceCodecError);
  second.feature_ordinal = 1;
  second.raw_system.feature_id = first.raw_system.feature_id;
  EXPECT_THROW(ov_msckf::CP2TraceCodec::EncodeRawSystemFile({first, second}),
               ov_msckf::CP2TraceCodecError);

  ov_msckf::CP2TraceDecodeLimits no_frames;
  no_frames.maximum_frames = 0U;
  EXPECT_THROW(ov_msckf::CP2TraceCodec::DecodeRawSystemFile(encoded.bytes, no_frames),
               ov_msckf::CP2TraceCodecError);

  ov_msckf::CP2TraceDecodeLimits aggregate_bytes;
  aggregate_bytes.maximum_file_bytes = encoded.bytes.size() - 1U;
  EXPECT_THROW(
      ov_msckf::CP2TraceCodec::DecodeRawSystemFile(encoded.bytes, aggregate_bytes),
      ov_msckf::CP2TraceCodecError);
  ov_msckf::CP2TraceDecodeLimits aggregate_coefficients;
  aggregate_coefficients.maximum_total_matrix_coefficients = 23U;
  EXPECT_THROW(ov_msckf::CP2TraceCodec::DecodeRawSystemFile(
                   encoded.bytes, aggregate_coefficients),
               ov_msckf::CP2TraceCodecError);
}

TEST(CP2TraceProposal, FrozenPayloadAndRoleFramingRejectAllStructuralCorruption) {
  ov_msckf::CP2ProposalTracePayload proposal;
  proposal.dx.resize(2);
  proposal.dx << -0.0, 1.5;
  proposal.P_plus.resize(2, 2);
  proposal.P_plus << 1.0, -0.0, -0.0, 2.0;

  const std::vector<std::uint8_t> payload =
      ov_msckf::CP2TraceCodec::EncodeProposalPayload(proposal);
  const std::string domain("SchurVIO-CP2-proposal-v1\0", 25U);
  ASSERT_GE(payload.size(), domain.size());
  EXPECT_TRUE(std::equal(domain.begin(), domain.end(), payload.begin()));
  EXPECT_EQ(payload.size(), 105U);
  ov_msckf::CP2Sha256 payload_sha256;
  payload_sha256.Update(payload);
  EXPECT_EQ(payload_sha256.HexDigest(),
            "4f0860eab24d84fe299b44459f5d4e3ecef4af2d3ff0a6be3cbd1414a1baaa5b");
  const ov_msckf::CP2ProposalTracePayload decoded_payload =
      ov_msckf::CP2TraceCodec::DecodeProposalPayload(payload);
  expect_matrix_bits_equal(decoded_payload.dx, proposal.dx);
  expect_matrix_bits_equal(decoded_payload.P_plus, proposal.P_plus);

  ov_msckf::CP2ProposalTraceFrame baseline;
  baseline.invocation = {2, 8, 1};
  baseline.role = ov_msckf::CP2ProposalTraceRole::kNullspaceBaseline;
  baseline.proposal = proposal;
  ov_msckf::CP2ProposalTraceFrame candidate = baseline;
  candidate.role = ov_msckf::CP2ProposalTraceRole::kSchurCandidate;

  const ov_msckf::CP2EncodedProposalFile candidate_only =
      ov_msckf::CP2TraceCodec::EncodeProposalFile({candidate});
  const std::vector<std::uint8_t> expected_candidate_file = hex_bytes(
      "536368757256494f2d4350322d70726f706f73616c2d66696c652d76310a000001000000000000000000000000000002"
      "000000000000000800000000000000010000000000000069536368757256494f2d4350322d70726f706f73616c2d7631"
      "000000000000000002000000000000000180000000000000003ff8000000000000000000000000000200000000000000"
      "023ff0000000000000800000000000000080000000000000004000000000000000");
  const std::vector<std::uint8_t> expected_candidate_header = hex_bytes(
      "0100000000000000000000000000000200000000000000080000000000000001"
      "0000000000000069");
  ASSERT_EQ(candidate_only.bytes.size(), expected_candidate_file.size());
  EXPECT_EQ(candidate_only.bytes, expected_candidate_file);
  ASSERT_GE(candidate_only.bytes.size(), 32U + expected_candidate_header.size());
  EXPECT_TRUE(std::equal(expected_candidate_header.begin(),
                         expected_candidate_header.end(),
                         candidate_only.bytes.begin() + 32));
  EXPECT_EQ(sha256_hex(candidate_only.bytes),
            "cee1dbc42e780aae9972359633cc7cc25512f695effd729e306aef00b41f3307");

  const ov_msckf::CP2EncodedProposalFile encoded =
      ov_msckf::CP2TraceCodec::EncodeProposalFile({baseline, candidate});
  ASSERT_EQ(encoded.payloads.size(), 2U);
  EXPECT_EQ(encoded.payloads[0].offset, 72U);
  const std::string magic("SchurVIO-CP2-proposal-file-v1\n\0\0", 32U);
  ASSERT_GE(encoded.bytes.size(), magic.size());
  EXPECT_TRUE(std::equal(magic.begin(), magic.end(), encoded.bytes.begin()));
  const std::vector<ov_msckf::CP2ProposalTraceFrame> decoded =
      ov_msckf::CP2TraceCodec::DecodeProposalFile(encoded.bytes);
  ASSERT_EQ(decoded.size(), 2U);
  EXPECT_EQ(decoded[0].role, ov_msckf::CP2ProposalTraceRole::kNullspaceBaseline);
  EXPECT_EQ(decoded[1].role, ov_msckf::CP2ProposalTraceRole::kSchurCandidate);

  std::vector<std::uint8_t> bad_reserved = encoded.bytes;
  bad_reserved[33U] = 1U;
  EXPECT_THROW(ov_msckf::CP2TraceCodec::DecodeProposalFile(bad_reserved),
               ov_msckf::CP2TraceCodecError);
  std::vector<std::uint8_t> bad_role = encoded.bytes;
  bad_role[32U] = 2U;
  EXPECT_THROW(ov_msckf::CP2TraceCodec::DecodeProposalFile(bad_role),
               ov_msckf::CP2TraceCodecError);
  EXPECT_THROW(ov_msckf::CP2TraceCodec::EncodeProposalFile({candidate, baseline}),
               ov_msckf::CP2TraceCodecError);
  EXPECT_THROW(ov_msckf::CP2TraceCodec::EncodeProposalFile({baseline, baseline}),
               ov_msckf::CP2TraceCodecError);

  std::vector<std::uint8_t> trailing = payload;
  trailing.push_back(0U);
  EXPECT_THROW(ov_msckf::CP2TraceCodec::DecodeProposalPayload(trailing),
               ov_msckf::CP2TraceCodecError);
  std::vector<std::uint8_t> dimensions_only(payload.begin(), payload.begin() + 41);
  ov_msckf::CP2TraceDecodeLimits preallocation_budget;
  preallocation_budget.maximum_total_matrix_coefficients = 1U;
  try {
    (void)ov_msckf::CP2TraceCodec::DecodeProposalPayload(
        dimensions_only, preallocation_budget);
    FAIL() << "aggregate dimension-header budget was not enforced";
  } catch (const ov_msckf::CP2TraceCodecError &error) {
    EXPECT_STREQ(error.what(),
                 "proposal dx exceeds configured aggregate coefficient limit");
  }
  ov_msckf::CP2ProposalTracePayload nonfinite = proposal;
  nonfinite.dx(0) = std::numeric_limits<double>::infinity();
  EXPECT_THROW(ov_msckf::CP2TraceCodec::EncodeProposalPayload(nonfinite),
               ov_msckf::CP2TraceCodecError);

  ov_msckf::CP2ProposalTracePayload empty;
  EXPECT_THROW(ov_msckf::CP2TraceCodec::EncodeProposalPayload(empty),
               ov_msckf::CP2TraceCodecError);
  ov_msckf::CP2ProposalTracePayload negative_diagonal = proposal;
  negative_diagonal.P_plus(0, 0) = -1.0;
  EXPECT_THROW(ov_msckf::CP2TraceCodec::EncodeProposalPayload(negative_diagonal),
               ov_msckf::CP2TraceCodecError);
  ov_msckf::CP2ProposalTracePayload asymmetric = proposal;
  asymmetric.P_plus(1, 0) = 0.25;
  EXPECT_THROW(ov_msckf::CP2TraceCodec::EncodeProposalPayload(asymmetric),
               ov_msckf::CP2TraceCodecError);
  ov_msckf::CP2ProposalTracePayload signed_zero_asymmetry = proposal;
  signed_zero_asymmetry.P_plus(0, 1) = 0.0;
  signed_zero_asymmetry.P_plus(1, 0) = -0.0;
  EXPECT_THROW(ov_msckf::CP2TraceCodec::EncodeProposalPayload(signed_zero_asymmetry),
               ov_msckf::CP2TraceCodecError);
  ov_msckf::CP2ProposalTracePayload negative_zero_diagonal = proposal;
  negative_zero_diagonal.P_plus(0, 0) = -0.0;
  EXPECT_NO_THROW(
      ov_msckf::CP2TraceCodec::EncodeProposalPayload(negative_zero_diagonal));

  ov_msckf::CP2TraceDecodeLimits aggregate_bytes;
  aggregate_bytes.maximum_file_bytes = encoded.bytes.size() - 1U;
  EXPECT_THROW(
      ov_msckf::CP2TraceCodec::DecodeProposalFile(encoded.bytes, aggregate_bytes),
      ov_msckf::CP2TraceCodecError);
  ov_msckf::CP2TraceDecodeLimits aggregate_coefficients;
  aggregate_coefficients.maximum_total_matrix_coefficients = 11U;
  EXPECT_THROW(ov_msckf::CP2TraceCodec::DecodeProposalFile(
                   encoded.bytes, aggregate_coefficients),
               ov_msckf::CP2TraceCodecError);
}

TEST(CP2TraceReplay, OwningDecodedFramesDriveTheSoleShadowMathKernel) {
  using ReplaySignature = ov_msckf::CP2TraceReplayResult (*)(
      const ov_msckf::CP2TraceReplayInput &);
  static_assert(
      std::is_same<decltype(&ov_msckf::CP2TraceCodec::ReplayInvocation),
                   ReplaySignature>::value,
      "offline replay must not copy complete artifact inputs before validation");
  const ov_msckf::CP2TraceReplayInput input = sealed_replay_fixture();

  const ov_msckf::CP2TraceReplayResult replay =
      ov_msckf::CP2TraceCodec::ReplayInvocation(input);
  ASSERT_TRUE(replay.math.input_valid);
  ASSERT_EQ(replay.math.features.size(), 2U);
  EXPECT_EQ(replay.math.nullspace.accepted_ids, (std::vector<std::uint64_t>{7, 3}));
  EXPECT_EQ(replay.math.schur.accepted_ids, (std::vector<std::uint64_t>{7, 3}));
  EXPECT_EQ(replay.baseline_accepted.set_sha256,
            "7c6f475259b73d0c3bb7b1ed2c885cc68d8f7c55e9a15409ca3f0466d4ac947a");
  EXPECT_EQ(replay.baseline_accepted.sequence_sha256,
            "309b2e00063377ddcffb0580b9f4f03f8a69ba84aaa388b09194aa4016792c0e");
  EXPECT_EQ(replay.candidate_accepted.set_sha256, replay.baseline_accepted.set_sha256);
  EXPECT_EQ(replay.candidate_accepted.sequence_sha256,
            replay.baseline_accepted.sequence_sha256);
  EXPECT_EQ(replay.raw_system_sha256[0], input.raw_frames[0].payload.sha256);
  EXPECT_EQ(replay.raw_system_sha256[1], input.raw_frames[1].payload.sha256);
  EXPECT_TRUE(replay.math.nullspace.proposal_available);
  EXPECT_TRUE(replay.math.schur.proposal_available);

  const ov_msckf::CP2ProposalTracePayload baseline{
      replay.math.nullspace.proposal.dx, replay.math.nullspace.proposal.P_plus};
  const std::vector<std::uint8_t> baseline_payload =
      ov_msckf::CP2TraceCodec::EncodeProposalPayload(baseline);
  const ov_msckf::CP2ProposalTracePayload candidate{
      replay.math.schur.proposal.dx, replay.math.schur.proposal.P_plus};
  const std::vector<std::uint8_t> candidate_payload =
      ov_msckf::CP2TraceCodec::EncodeProposalPayload(candidate);
  EXPECT_EQ(
      bytes_hex(baseline_payload),
      "536368757256494f2d4350322d70726f706f73616c2d763100000000000000000200000000000000013fa6d70b9f594b69"
      "bfb16ee960e40406000000000000000200000000000000023febf233a483b6273f99759dcf5f55fe3f99759dcf5f55fe"
      "3fec20aa8ed09c16");
  EXPECT_EQ(sha256_hex(baseline_payload),
            "8c1c051575564b0405741a63b0ab8308fc4c4b901a3ae1d38b0d677c375b6f2d");
  EXPECT_EQ(
      bytes_hex(candidate_payload),
      "536368757256494f2d4350322d70726f706f73616c2d763100000000000000000200000000000000013fa6d70b9f594b69"
      "bfb16ee960e40408000000000000000200000000000000023febf233a483b6273f99759dcf5f56043f99759dcf5f5604"
      "3fec20aa8ed09c16");
  EXPECT_EQ(sha256_hex(candidate_payload),
            "12e940d0fe01651d93636bd4cbba522c1526dd47749bd3eb3d106822f23db4c8");
  EXPECT_NE(baseline_payload, candidate_payload);
}

TEST(CP2TraceReplay, ContextDigestAndPriorLayoutDisconnectsFailBeforeMath) {
  ov_msckf::CP2TraceReplayInput wrong_context = sealed_replay_fixture();
  wrong_context.raw_frames[1].invocation.invocation_id += 1U;
  EXPECT_THROW(ov_msckf::CP2TraceCodec::ReplayInvocation(wrong_context),
               ov_msckf::CP2TraceCodecError);

  ov_msckf::CP2TraceReplayInput empty_digest = sealed_replay_fixture();
  empty_digest.raw_frames[0].payload.sha256.clear();
  EXPECT_THROW(ov_msckf::CP2TraceCodec::ReplayInvocation(empty_digest),
               ov_msckf::CP2TraceCodecError);

  ov_msckf::CP2TraceReplayInput zero_length = sealed_replay_fixture();
  zero_length.raw_frames[0].payload.length = 0U;
  EXPECT_THROW(ov_msckf::CP2TraceCodec::ReplayInvocation(zero_length),
               ov_msckf::CP2TraceCodecError);

  ov_msckf::CP2TraceReplayInput wrong_offset = sealed_replay_fixture();
  wrong_offset.raw_frames[0].payload.offset = 79U;
  EXPECT_THROW(ov_msckf::CP2TraceCodec::ReplayInvocation(wrong_offset),
               ov_msckf::CP2TraceCodecError);

  ov_msckf::CP2TraceReplayInput wrong_length = sealed_replay_fixture();
  wrong_length.raw_frames[0].payload.length += 1U;
  EXPECT_THROW(ov_msckf::CP2TraceCodec::ReplayInvocation(wrong_length),
               ov_msckf::CP2TraceCodecError);

  ov_msckf::CP2TraceReplayInput wrong_digest = sealed_replay_fixture();
  wrong_digest.raw_frames[0].payload.sha256 = std::string(64U, '0');
  EXPECT_THROW(ov_msckf::CP2TraceCodec::ReplayInvocation(wrong_digest),
               ov_msckf::CP2TraceCodecError);

  ov_msckf::CP2TraceReplayInput uppercase_digest = sealed_replay_fixture();
  uppercase_digest.raw_frames[0].payload.sha256[0] = 'A';
  EXPECT_THROW(ov_msckf::CP2TraceCodec::ReplayInvocation(uppercase_digest),
               ov_msckf::CP2TraceCodecError);

  ov_msckf::CP2TraceReplayInput empty_proposal_digest = sealed_replay_fixture();
  empty_proposal_digest.proposal_frames[0].payload.sha256.clear();
  EXPECT_THROW(ov_msckf::CP2TraceCodec::ReplayInvocation(empty_proposal_digest),
               ov_msckf::CP2TraceCodecError);

  ov_msckf::CP2TraceReplayInput prior_disconnect = sealed_replay_fixture();
  prior_disconnect.prior.state_blocks = {{0, 1, 0}, {1, 1, 1}};
  EXPECT_THROW(ov_msckf::CP2TraceCodec::ReplayInvocation(prior_disconnect),
               ov_msckf::CP2TraceCodecError);

  ov_msckf::CP2TraceReplayInput duplicate = sealed_replay_fixture();
  duplicate.raw_frames[1].raw_system.feature_id =
      duplicate.raw_frames[0].raw_system.feature_id;
  EXPECT_THROW(ov_msckf::CP2TraceCodec::ReplayInvocation(duplicate),
               ov_msckf::CP2TraceCodecError);

  ov_msckf::CP2TraceReplayInput mutated_candidate = sealed_replay_fixture();
  mutated_candidate.proposal_frames[1].proposal.dx(0) = std::nextafter(
      mutated_candidate.proposal_frames[1].proposal.dx(0),
      std::numeric_limits<double>::infinity());
  const ov_msckf::CP2EncodedProposalFile mutated_candidate_file =
      ov_msckf::CP2TraceCodec::EncodeProposalFile(
          mutated_candidate.proposal_frames);
  mutated_candidate.proposal_file_bytes = mutated_candidate_file.bytes;
  mutated_candidate.proposal_frames = ov_msckf::CP2TraceCodec::DecodeProposalFile(
      mutated_candidate.proposal_file_bytes);
  EXPECT_THROW(ov_msckf::CP2TraceCodec::ReplayInvocation(mutated_candidate),
               ov_msckf::CP2TraceCodecError);

  ov_msckf::CP2TraceReplayInput candidate_copied_from_baseline =
      sealed_replay_fixture();
  candidate_copied_from_baseline.proposal_frames[1].proposal =
      candidate_copied_from_baseline.proposal_frames[0].proposal;
  const ov_msckf::CP2EncodedProposalFile copied_candidate_file =
      ov_msckf::CP2TraceCodec::EncodeProposalFile(
          candidate_copied_from_baseline.proposal_frames);
  candidate_copied_from_baseline.proposal_file_bytes =
      copied_candidate_file.bytes;
  candidate_copied_from_baseline.proposal_frames =
      ov_msckf::CP2TraceCodec::DecodeProposalFile(
          candidate_copied_from_baseline.proposal_file_bytes);
  EXPECT_THROW(
      ov_msckf::CP2TraceCodec::ReplayInvocation(candidate_copied_from_baseline),
      ov_msckf::CP2TraceCodecError);

  ov_msckf::CP2TraceReplayInput proposal_context_disconnect =
      sealed_replay_fixture();
  proposal_context_disconnect.proposal_frames[1].invocation.invocation_id += 1U;
  const ov_msckf::CP2EncodedProposalFile disconnected_proposal_file =
      ov_msckf::CP2TraceCodec::EncodeProposalFile(
          proposal_context_disconnect.proposal_frames);
  proposal_context_disconnect.proposal_file_bytes =
      disconnected_proposal_file.bytes;
  proposal_context_disconnect.proposal_frames =
      ov_msckf::CP2TraceCodec::DecodeProposalFile(
          proposal_context_disconnect.proposal_file_bytes);
  EXPECT_THROW(
      ov_msckf::CP2TraceCodec::ReplayInvocation(proposal_context_disconnect),
      ov_msckf::CP2TraceCodecError);

  ov_msckf::CP2TraceReplayInput raw_proposal_disconnect =
      sealed_replay_fixture();
  raw_proposal_disconnect.raw_frames[0].raw_system.residual(5) += 0.01;
  const ov_msckf::CP2EncodedRawSystemFile disconnected_raw_file =
      ov_msckf::CP2TraceCodec::EncodeRawSystemFile(
          raw_proposal_disconnect.raw_frames);
  raw_proposal_disconnect.raw_system_file_bytes = disconnected_raw_file.bytes;
  raw_proposal_disconnect.raw_frames = ov_msckf::CP2TraceCodec::DecodeRawSystemFile(
      raw_proposal_disconnect.raw_system_file_bytes);
  EXPECT_THROW(ov_msckf::CP2TraceCodec::ReplayInvocation(raw_proposal_disconnect),
               ov_msckf::CP2TraceCodecError);
}

} // namespace
