/*
 * SchurVIO-Lite CP2 canonical trace codec and value-only replay boundary.
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include "CP2TraceCodec.h"

#include "CP2Canonical.h"

#include <algorithm>
#include <cmath>
#include <cstring>
#include <limits>
#include <set>
#include <tuple>
#include <utility>

namespace ov_msckf {
namespace {

constexpr char kRawSystemDomain[] = "SchurVIO-CP2-raw-system-v1";
constexpr char kProposalDomain[] = "SchurVIO-CP2-proposal-v1";
constexpr char kAcceptedSetDomain[] = "SchurVIO-CP2-accepted-set-v1";
constexpr char kAcceptedSequenceDomain[] = "SchurVIO-CP2-accepted-sequence-v1";
constexpr char kRawFileMagic[] = "SchurVIO-CP2-raw-file-v1\n\0\0\0\0\0\0\0";
constexpr char kProposalFileMagic[] = "SchurVIO-CP2-proposal-file-v1\n\0\0";

static_assert(sizeof(kRawFileMagic) - 1U == 32U, "CP2 raw file magic must be 32 bytes");
static_assert(sizeof(kProposalFileMagic) - 1U == 32U,
              "CP2 proposal file magic must be 32 bytes");

std::uint64_t checked_u64(std::size_t value, const char *what) {
  if (value > std::numeric_limits<std::uint64_t>::max()) {
    throw CP2TraceCodecError(std::string(what) + " exceeds u64");
  }
  return static_cast<std::uint64_t>(value);
}

std::uint64_t checked_u64(Eigen::Index value, const char *what) {
  if (value < 0 || static_cast<std::uintmax_t>(value) >
                       static_cast<std::uintmax_t>(std::numeric_limits<std::uint64_t>::max())) {
    throw CP2TraceCodecError(std::string(what) + " is not a u64");
  }
  return static_cast<std::uint64_t>(value);
}

std::string sha256_hex(const std::vector<std::uint8_t> &bytes) {
  CP2Sha256 sha256;
  sha256.Update(bytes);
  return sha256.HexDigest();
}

std::uint64_t binary64_bits(double value) noexcept {
  std::uint64_t bits = 0U;
  std::memcpy(&bits, &value, sizeof(bits));
  return bits;
}

bool is_lowercase_sha256(const std::string &value) noexcept {
  if (value.size() != 64U) {
    return false;
  }
  for (char character : value) {
    if (!((character >= '0' && character <= '9') ||
          (character >= 'a' && character <= 'f'))) {
      return false;
    }
  }
  return true;
}

void append_domain(CP2CanonicalBuffer &output, const char *domain, std::size_t bytes) {
  // sizeof(domain literal) includes the one contract-required zero byte.
  output.AppendRawBytes(domain, bytes);
}

class CanonicalReader {
public:
  CanonicalReader(const std::vector<std::uint8_t> &bytes, const CP2TraceDecodeLimits &limits,
                  bool enforce_payload_limit, std::uint64_t &aggregate_coefficients)
      : bytes_(bytes), limits_(limits), aggregate_coefficients_(aggregate_coefficients) {
    if (enforce_payload_limit && checked_u64(bytes.size(), "payload size") > limits.maximum_payload_bytes) {
      throw CP2TraceCodecError("CP2 payload exceeds configured byte limit");
    }
  }

  std::size_t position() const noexcept { return position_; }
  std::size_t remaining() const noexcept { return bytes_.size() - position_; }
  bool empty() const noexcept { return position_ == bytes_.size(); }

  void Expect(const void *expected, std::size_t size, const char *what) {
    Require(size, what);
    if (size > 0U && std::memcmp(bytes_.data() + position_, expected, size) != 0) {
      throw CP2TraceCodecError(std::string("invalid ") + what);
    }
    position_ += size;
  }

  std::uint8_t ReadU8(const char *what) {
    Require(1U, what);
    return bytes_[position_++];
  }

  std::uint64_t ReadU64(const char *what) {
    Require(8U, what);
    std::uint64_t value = 0U;
    for (std::size_t index = 0; index < 8U; ++index) {
      value = (value << 8U) | bytes_[position_ + index];
    }
    position_ += 8U;
    return value;
  }

  double ReadBinary64(const char *what) {
    const std::uint64_t bits = ReadU64(what);
    double value = 0.0;
    static_assert(sizeof(value) == sizeof(bits), "CP2 requires IEEE-754 binary64");
    std::memcpy(&value, &bits, sizeof(value));
    return value;
  }

  Eigen::MatrixXd ReadMatrix(const char *what) {
    const std::uint64_t rows = ReadU64(what);
    const std::uint64_t columns = ReadU64(what);
    const std::uint64_t coefficients = CheckedCoefficientCount(rows, columns, what);
    ConsumeCoefficients(coefficients, what);
    RequireCoefficientBytes(coefficients, what);

    Eigen::MatrixXd result(CheckedEigenIndex(rows, what), CheckedEigenIndex(columns, what));
    for (Eigen::Index row = 0; row < result.rows(); ++row) {
      for (Eigen::Index column = 0; column < result.cols(); ++column) {
        result(row, column) = ReadBinary64(what);
      }
    }
    return result;
  }

  Eigen::VectorXd ReadVector(const char *what) {
    const std::uint64_t rows = ReadU64(what);
    const std::uint64_t columns = ReadU64(what);
    if (columns != 1U || rows > limits_.maximum_vector_rows) {
      throw CP2TraceCodecError(std::string(what) + " is not a bounded column vector");
    }
    const std::uint64_t coefficients = CheckedCoefficientCount(rows, columns, what);
    ConsumeCoefficients(coefficients, what);
    RequireCoefficientBytes(coefficients, what);

    Eigen::VectorXd result(CheckedEigenIndex(rows, what));
    for (Eigen::Index row = 0; row < result.rows(); ++row) {
      result(row) = ReadBinary64(what);
    }
    return result;
  }

  std::vector<std::uint8_t> ReadBytes(std::uint64_t count, const char *what) {
    if (count > limits_.maximum_payload_bytes || count > std::numeric_limits<std::size_t>::max()) {
      throw CP2TraceCodecError(std::string(what) + " exceeds configured byte limit");
    }
    const std::size_t size = static_cast<std::size_t>(count);
    Require(size, what);
    std::vector<std::uint8_t> result(bytes_.begin() + static_cast<std::ptrdiff_t>(position_),
                                     bytes_.begin() + static_cast<std::ptrdiff_t>(position_ + size));
    position_ += size;
    return result;
  }

private:
  void Require(std::size_t size, const char *what) const {
    if (size > bytes_.size() - position_) {
      throw CP2TraceCodecError(std::string("truncated ") + what);
    }
  }

  Eigen::Index CheckedEigenIndex(std::uint64_t value, const char *what) const {
    if (value > static_cast<std::uintmax_t>(std::numeric_limits<Eigen::Index>::max())) {
      throw CP2TraceCodecError(std::string(what) + " dimension exceeds Eigen::Index");
    }
    return static_cast<Eigen::Index>(value);
  }

  std::uint64_t CheckedCoefficientCount(std::uint64_t rows, std::uint64_t columns,
                                        const char *what) const {
    if (rows != 0U && columns > std::numeric_limits<std::uint64_t>::max() / rows) {
      throw CP2TraceCodecError(std::string(what) + " coefficient count overflows u64");
    }
    const std::uint64_t coefficients = rows * columns;
    if (coefficients > limits_.maximum_matrix_coefficients) {
      throw CP2TraceCodecError(std::string(what) + " exceeds configured coefficient limit");
    }
    CheckedEigenIndex(rows, what);
    CheckedEigenIndex(columns, what);
    return coefficients;
  }

  void RequireCoefficientBytes(std::uint64_t coefficients, const char *what) const {
    if (coefficients > std::numeric_limits<std::size_t>::max() / 8U) {
      throw CP2TraceCodecError(std::string(what) + " byte count exceeds size_t");
    }
    Require(static_cast<std::size_t>(coefficients) * 8U, what);
  }

  void ConsumeCoefficients(std::uint64_t coefficients, const char *what) {
    const std::uint64_t limit = limits_.maximum_total_matrix_coefficients;
    if (coefficients > limit || aggregate_coefficients_ > limit - coefficients) {
      throw CP2TraceCodecError(std::string(what) +
                               " exceeds configured aggregate coefficient limit");
    }
    aggregate_coefficients_ += coefficients;
  }

  const std::vector<std::uint8_t> &bytes_;
  const CP2TraceDecodeLimits &limits_;
  std::uint64_t &aggregate_coefficients_;
  std::size_t position_ = 0U;
};

using RawFrameKey = std::tuple<std::uint64_t, std::uint64_t, std::uint64_t,
                               std::uint64_t, std::uint64_t>;
using ProposalFrameKey =
    std::tuple<std::uint64_t, std::uint64_t, std::uint64_t, std::uint8_t>;

RawFrameKey raw_frame_key(const CP2RawSystemTraceFrame &frame) {
  return std::make_tuple(frame.invocation.sequence_index, frame.invocation.pair_index,
                         frame.invocation.invocation_id, frame.feature_ordinal,
                         frame.raw_system.feature_id);
}

std::uint8_t proposal_role_value(CP2ProposalTraceRole role) {
  switch (role) {
  case CP2ProposalTraceRole::kNullspaceBaseline:
    return 0U;
  case CP2ProposalTraceRole::kSchurCandidate:
    return 1U;
  }
  throw CP2TraceCodecError("invalid CP2 proposal role");
}

ProposalFrameKey proposal_frame_key(const CP2ProposalTraceFrame &frame) {
  return std::make_tuple(frame.invocation.sequence_index, frame.invocation.pair_index,
                         frame.invocation.invocation_id, proposal_role_value(frame.role));
}

void validate_raw_layout_encoding(const CP2RawFeatureSystem &raw) {
  if (raw.H_x.rows() != raw.H_f.rows() || raw.residual.rows() != raw.H_x.rows() ||
      raw.residual.cols() != 1) {
    throw CP2TraceCodecError("raw system matrix and residual row dimensions disagree");
  }

  Eigen::Index expected_offset = 0;
  std::set<Eigen::Index> covariance_ids;
  std::vector<std::pair<Eigen::Index, Eigen::Index>> covariance_ranges;
  for (const CP2FeatureGateLayoutBlock &block : raw.jacobian_layout) {
    if (block.covariance_id < 0 || block.size <= 0 || block.H_offset != expected_offset ||
        block.size > raw.H_x.cols() - expected_offset) {
      throw CP2TraceCodecError("raw Jacobian layout is not an exact ordered column partition");
    }
    if (!covariance_ids.insert(block.covariance_id).second ||
        block.size > std::numeric_limits<Eigen::Index>::max() - block.covariance_id) {
      throw CP2TraceCodecError("raw Jacobian layout has a duplicate or overflowing covariance ID");
    }
    const Eigen::Index end = block.covariance_id + block.size;
    for (const auto &range : covariance_ranges) {
      if (block.covariance_id < range.second && range.first < end) {
        throw CP2TraceCodecError("raw Jacobian layout covariance ranges overlap");
      }
    }
    covariance_ranges.emplace_back(block.covariance_id, end);
    expected_offset += block.size;
  }
  if (expected_offset != raw.H_x.cols()) {
    throw CP2TraceCodecError("raw Jacobian layout does not cover every H_x column");
  }
}

void validate_proposal(const CP2ProposalTracePayload &proposal) {
  if (proposal.dx.cols() != 1 || proposal.dx.rows() <= 0 ||
      proposal.P_plus.rows() != proposal.P_plus.cols() ||
      proposal.dx.rows() != proposal.P_plus.rows()) {
    throw CP2TraceCodecError("proposal dx and P_plus dimensions disagree");
  }
  if (!proposal.dx.allFinite() || !proposal.P_plus.allFinite()) {
    throw CP2TraceCodecError("accepted proposal payload must be finite");
  }
  for (Eigen::Index row = 0; row < proposal.P_plus.rows(); ++row) {
    // The production preview rejects only `diagonal < 0.0`; IEEE-754 -0.0
    // compares equal to zero and is therefore an accepted nonnegative value.
    if (proposal.P_plus(row, row) < 0.0) {
      throw CP2TraceCodecError("accepted proposal covariance has a negative diagonal");
    }
    for (Eigen::Index column = 0; column < row; ++column) {
      if (binary64_bits(proposal.P_plus(row, column)) !=
          binary64_bits(proposal.P_plus(column, row))) {
        throw CP2TraceCodecError(
            "accepted proposal covariance is not bitwise mirrored");
      }
    }
  }
}

void validate_payload_reference(const CP2TracePayloadReference &reference,
                                const std::vector<std::uint8_t> &file,
                                const std::vector<std::uint8_t> &canonical_payload,
                                std::uint64_t minimum_offset,
                                const char *what) {
  if (!is_lowercase_sha256(reference.sha256)) {
    throw CP2TraceCodecError(std::string(what) + " has no exact lowercase SHA-256");
  }
  if (reference.length == 0U || reference.length != canonical_payload.size()) {
    throw CP2TraceCodecError(std::string(what) + " has no exact nonzero canonical length");
  }
  if (reference.offset < minimum_offset || reference.offset > file.size() ||
      reference.length > static_cast<std::uint64_t>(file.size() -
                                                    static_cast<std::size_t>(reference.offset))) {
    throw CP2TraceCodecError(std::string(what) + " offset/length is outside its retained file");
  }
  const auto first = file.begin() + static_cast<std::ptrdiff_t>(reference.offset);
  if (!std::equal(canonical_payload.begin(), canonical_payload.end(), first)) {
    throw CP2TraceCodecError(std::string(what) + " does not select its canonical payload bytes");
  }
  if (reference.sha256 != sha256_hex(canonical_payload)) {
    throw CP2TraceCodecError(std::string(what) + " SHA-256 does not match its payload bytes");
  }
}

void validate_raw_frame_sequence(const std::vector<CP2RawSystemTraceFrame> &frames) {
  bool have_previous = false;
  RawFrameKey previous;
  CP2InvocationTraceIdentity active_invocation;
  std::uint64_t expected_ordinal = 0U;
  std::set<std::uint64_t> active_feature_ids;

  for (const CP2RawSystemTraceFrame &frame : frames) {
    const RawFrameKey key = raw_frame_key(frame);
    if (have_previous && !(previous < key)) {
      throw CP2TraceCodecError("raw-system frames are not in strict lexicographic order");
    }
    if (!have_previous || frame.invocation != active_invocation) {
      active_invocation = frame.invocation;
      expected_ordinal = 0U;
      active_feature_ids.clear();
    }
    if (frame.feature_ordinal != expected_ordinal) {
      throw CP2TraceCodecError("raw-system feature ordinals are not contiguous from zero");
    }
    if (!active_feature_ids.insert(frame.raw_system.feature_id).second) {
      throw CP2TraceCodecError("duplicate feature ID within one invocation");
    }
    if (expected_ordinal == std::numeric_limits<std::uint64_t>::max()) {
      throw CP2TraceCodecError("raw-system feature ordinal overflows u64");
    }
    ++expected_ordinal;
    previous = key;
    have_previous = true;
  }
}

void validate_proposal_frame_sequence(const std::vector<CP2ProposalTraceFrame> &frames) {
  bool have_previous = false;
  ProposalFrameKey previous;
  for (const CP2ProposalTraceFrame &frame : frames) {
    const ProposalFrameKey key = proposal_frame_key(frame);
    if (have_previous && !(previous < key)) {
      throw CP2TraceCodecError("proposal frames are not in strict lexicographic order");
    }
    previous = key;
    have_previous = true;
  }
}

void validate_replay_prior_and_layout(const CP2TraceReplayInput &input) {
  const CP2TraceDecodeLimits limits;
  const Eigen::Index dimension = input.prior.covariance.rows();
  if (dimension <= 0 || input.prior.covariance.cols() != dimension ||
      !input.prior.covariance.allFinite() || input.prior.state_blocks.empty()) {
    throw CP2TraceCodecError("replay prior is not a finite square owning snapshot");
  }
  if (checked_u64(input.prior.covariance.size(), "replay prior coefficient count") >
          limits.maximum_matrix_coefficients ||
      checked_u64(input.prior.state_blocks.size(), "replay prior block count") >
          limits.maximum_layout_blocks) {
    throw CP2TraceCodecError("replay prior exceeds the bounded value-only input limits");
  }
  if (!std::isfinite(input.sigma_px) || !(input.sigma_px > 0.0) ||
      !std::isfinite(input.sigma_px_sq) || !(input.sigma_px_sq > 0.0) ||
      input.sigma_px_sq != input.sigma_px * input.sigma_px ||
      !std::isfinite(input.chi2_multiplier) ||
      input.chi_squared_table.size() != 499U) {
    throw CP2TraceCodecError("replay scalar/table inputs violate the frozen startup contract");
  }
  for (int degrees_of_freedom = 1; degrees_of_freedom < 500;
       ++degrees_of_freedom) {
    const auto entry = input.chi_squared_table.find(degrees_of_freedom);
    if (entry == input.chi_squared_table.end() || !std::isfinite(entry->second)) {
      throw CP2TraceCodecError(
          "replay scalar/table inputs violate the frozen startup contract");
    }
  }
  Eigen::Index expected_offset = 0;
  for (const MSCKFUpdatePreviewBlock &block : input.prior.state_blocks) {
    if (block.covariance_id != expected_offset || block.offset != expected_offset ||
        block.size <= 0 || block.size > dimension - expected_offset) {
      throw CP2TraceCodecError("replay prior blocks are not a complete ordered partition");
    }
    expected_offset += block.size;
  }
  if (expected_offset != dimension) {
    throw CP2TraceCodecError("replay prior blocks do not cover the covariance");
  }

  for (const CP2RawSystemTraceFrame &frame : input.raw_frames) {
    for (const CP2FeatureGateLayoutBlock &layout : frame.raw_system.jacobian_layout) {
      bool exact_match = false;
      for (const MSCKFUpdatePreviewBlock &state_block : input.prior.state_blocks) {
        if (layout.covariance_id == state_block.covariance_id &&
            layout.size == state_block.size) {
          exact_match = true;
          break;
        }
      }
      if (!exact_match || layout.size > dimension - layout.covariance_id) {
        throw CP2TraceCodecError("raw/prior layout identity disconnect");
      }
    }
  }
}

CP2RawFeatureSystem decode_raw_system_payload(
    const std::vector<std::uint8_t> &payload,
    const CP2TraceDecodeLimits &limits,
    std::uint64_t &aggregate_coefficients) {
  CanonicalReader input(payload, limits, true, aggregate_coefficients);
  input.Expect(kRawSystemDomain, sizeof(kRawSystemDomain), "raw-system domain");

  CP2RawFeatureSystem raw;
  raw.feature_id = input.ReadU64("raw feature ID");
  raw.H_x = input.ReadMatrix("raw H_x");
  raw.H_f = input.ReadMatrix("raw H_f");
  raw.residual = input.ReadVector("raw residual");
  const std::uint64_t layout_count = input.ReadU64("raw layout count");
  if (layout_count > limits.maximum_layout_blocks ||
      layout_count > std::numeric_limits<std::size_t>::max() ||
      layout_count > static_cast<std::uint64_t>(input.remaining() / 16U)) {
    throw CP2TraceCodecError("raw layout count exceeds configured limit");
  }
  raw.jacobian_layout.reserve(static_cast<std::size_t>(layout_count));
  Eigen::Index H_offset = 0;
  for (std::uint64_t index = 0; index < layout_count; ++index) {
    const std::uint64_t covariance_id = input.ReadU64("raw layout covariance ID");
    const std::uint64_t size = input.ReadU64("raw layout size");
    if (covariance_id > static_cast<std::uintmax_t>(
                            std::numeric_limits<Eigen::Index>::max()) ||
        size > static_cast<std::uintmax_t>(
                   std::numeric_limits<Eigen::Index>::max()) ||
        size > static_cast<std::uint64_t>(
                   std::numeric_limits<Eigen::Index>::max() - H_offset)) {
      throw CP2TraceCodecError("raw layout metadata exceeds Eigen::Index");
    }
    raw.jacobian_layout.push_back({static_cast<Eigen::Index>(covariance_id),
                                   static_cast<Eigen::Index>(size), H_offset});
    H_offset += static_cast<Eigen::Index>(size);
  }
  if (!input.empty()) {
    throw CP2TraceCodecError("raw-system payload has trailing bytes");
  }
  validate_raw_layout_encoding(raw);
  return raw;
}

CP2ProposalTracePayload decode_proposal_payload(
    const std::vector<std::uint8_t> &payload,
    const CP2TraceDecodeLimits &limits,
    std::uint64_t &aggregate_coefficients) {
  CanonicalReader input(payload, limits, true, aggregate_coefficients);
  input.Expect(kProposalDomain, sizeof(kProposalDomain), "proposal domain");
  CP2ProposalTracePayload proposal;
  proposal.dx = input.ReadVector("proposal dx");
  proposal.P_plus = input.ReadMatrix("proposal P_plus");
  if (!input.empty()) {
    throw CP2TraceCodecError("proposal payload has trailing bytes");
  }
  validate_proposal(proposal);
  return proposal;
}

} // namespace

bool operator==(const CP2InvocationTraceIdentity &left,
                const CP2InvocationTraceIdentity &right) noexcept {
  return left.sequence_index == right.sequence_index && left.pair_index == right.pair_index &&
         left.invocation_id == right.invocation_id;
}

bool operator!=(const CP2InvocationTraceIdentity &left,
                const CP2InvocationTraceIdentity &right) noexcept {
  return !(left == right);
}

std::vector<std::uint8_t>
CP2TraceCodec::EncodeRawSystemPayload(const CP2RawFeatureSystem &raw_system) {
  validate_raw_layout_encoding(raw_system);
  CP2CanonicalBuffer output;
  append_domain(output, kRawSystemDomain, sizeof(kRawSystemDomain));
  output.AppendU64(raw_system.feature_id);
  output.AppendMatrix(raw_system.H_x);
  output.AppendMatrix(raw_system.H_f);
  output.AppendVector(raw_system.residual);
  output.AppendU64(checked_u64(raw_system.jacobian_layout.size(), "raw layout count"));
  for (const CP2FeatureGateLayoutBlock &block : raw_system.jacobian_layout) {
    output.AppendU64(checked_u64(block.covariance_id, "raw covariance ID"));
    output.AppendU64(checked_u64(block.size, "raw layout size"));
  }
  return output.bytes();
}

CP2RawFeatureSystem
CP2TraceCodec::DecodeRawSystemPayload(const std::vector<std::uint8_t> &payload,
                                      const CP2TraceDecodeLimits &limits) {
  std::uint64_t aggregate_coefficients = 0U;
  return decode_raw_system_payload(payload, limits, aggregate_coefficients);
}

std::vector<std::uint8_t>
CP2TraceCodec::EncodeProposalPayload(const CP2ProposalTracePayload &proposal) {
  validate_proposal(proposal);
  CP2CanonicalBuffer output;
  append_domain(output, kProposalDomain, sizeof(kProposalDomain));
  output.AppendVector(proposal.dx);
  output.AppendMatrix(proposal.P_plus);
  return output.bytes();
}

CP2ProposalTracePayload
CP2TraceCodec::DecodeProposalPayload(const std::vector<std::uint8_t> &payload,
                                     const CP2TraceDecodeLimits &limits) {
  std::uint64_t aggregate_coefficients = 0U;
  return decode_proposal_payload(payload, limits, aggregate_coefficients);
}

CP2EncodedRawSystemFile
CP2TraceCodec::EncodeRawSystemFile(const std::vector<CP2RawSystemTraceFrame> &frames) {
  validate_raw_frame_sequence(frames);
  CP2CanonicalBuffer output;
  output.AppendRawBytes(kRawFileMagic, sizeof(kRawFileMagic) - 1U);
  CP2EncodedRawSystemFile encoded;
  encoded.payloads.reserve(frames.size());

  for (const CP2RawSystemTraceFrame &frame : frames) {
    const std::vector<std::uint8_t> payload = EncodeRawSystemPayload(frame.raw_system);
    output.AppendU64(frame.invocation.sequence_index);
    output.AppendU64(frame.invocation.pair_index);
    output.AppendU64(frame.invocation.invocation_id);
    output.AppendU64(frame.feature_ordinal);
    output.AppendU64(frame.raw_system.feature_id);
    output.AppendU64(checked_u64(payload.size(), "raw payload length"));

    CP2TracePayloadReference reference;
    reference.offset = checked_u64(output.size(), "raw payload offset");
    reference.length = checked_u64(payload.size(), "raw payload length");
    reference.sha256 = sha256_hex(payload);
    encoded.payloads.push_back(reference);
    output.AppendRawBytes(payload);
  }
  encoded.bytes = output.bytes();
  return encoded;
}

std::vector<CP2RawSystemTraceFrame>
CP2TraceCodec::DecodeRawSystemFile(const std::vector<std::uint8_t> &file,
                                   const CP2TraceDecodeLimits &limits) {
  if (checked_u64(file.size(), "raw file size") > limits.maximum_file_bytes) {
    throw CP2TraceCodecError("raw-system file exceeds configured aggregate byte limit");
  }
  std::uint64_t total_coefficients = 0U;
  CanonicalReader input(file, limits, false, total_coefficients);
  input.Expect(kRawFileMagic, sizeof(kRawFileMagic) - 1U, "raw file domain");
  std::vector<CP2RawSystemTraceFrame> frames;
  while (!input.empty()) {
    if (frames.size() >= limits.maximum_frames) {
      throw CP2TraceCodecError("raw-system frame count exceeds configured limit");
    }
    CP2RawSystemTraceFrame frame;
    frame.invocation.sequence_index = input.ReadU64("raw frame sequence index");
    frame.invocation.pair_index = input.ReadU64("raw frame pair index");
    frame.invocation.invocation_id = input.ReadU64("raw frame invocation ID");
    frame.feature_ordinal = input.ReadU64("raw frame feature ordinal");
    const std::uint64_t header_feature_id = input.ReadU64("raw frame feature ID");
    const std::uint64_t payload_length = input.ReadU64("raw frame payload length");
    frame.payload.offset = checked_u64(input.position(), "raw payload offset");
    frame.payload.length = payload_length;
    const std::vector<std::uint8_t> payload = input.ReadBytes(payload_length, "raw frame payload");
    frame.payload.sha256 = sha256_hex(payload);
    frame.raw_system =
        decode_raw_system_payload(payload, limits, total_coefficients);
    if (frame.raw_system.feature_id != header_feature_id) {
      throw CP2TraceCodecError("raw frame feature ID does not match its canonical payload");
    }
    frames.push_back(std::move(frame));
  }
  validate_raw_frame_sequence(frames);
  return frames;
}

CP2EncodedProposalFile
CP2TraceCodec::EncodeProposalFile(const std::vector<CP2ProposalTraceFrame> &frames) {
  validate_proposal_frame_sequence(frames);
  CP2CanonicalBuffer output;
  output.AppendRawBytes(kProposalFileMagic, sizeof(kProposalFileMagic) - 1U);
  CP2EncodedProposalFile encoded;
  encoded.payloads.reserve(frames.size());

  for (const CP2ProposalTraceFrame &frame : frames) {
    const std::vector<std::uint8_t> payload = EncodeProposalPayload(frame.proposal);
    const std::uint8_t role = proposal_role_value(frame.role);
    output.AppendRawBytes(&role, 1U);
    const std::uint8_t reserved[7] = {0U, 0U, 0U, 0U, 0U, 0U, 0U};
    output.AppendRawBytes(reserved, sizeof(reserved));
    output.AppendU64(frame.invocation.sequence_index);
    output.AppendU64(frame.invocation.pair_index);
    output.AppendU64(frame.invocation.invocation_id);
    output.AppendU64(checked_u64(payload.size(), "proposal payload length"));

    CP2TracePayloadReference reference;
    reference.offset = checked_u64(output.size(), "proposal payload offset");
    reference.length = checked_u64(payload.size(), "proposal payload length");
    reference.sha256 = sha256_hex(payload);
    encoded.payloads.push_back(reference);
    output.AppendRawBytes(payload);
  }
  encoded.bytes = output.bytes();
  return encoded;
}

std::vector<CP2ProposalTraceFrame>
CP2TraceCodec::DecodeProposalFile(const std::vector<std::uint8_t> &file,
                                  const CP2TraceDecodeLimits &limits) {
  if (checked_u64(file.size(), "proposal file size") > limits.maximum_file_bytes) {
    throw CP2TraceCodecError("proposal file exceeds configured aggregate byte limit");
  }
  std::uint64_t total_coefficients = 0U;
  CanonicalReader input(file, limits, false, total_coefficients);
  input.Expect(kProposalFileMagic, sizeof(kProposalFileMagic) - 1U,
               "proposal file domain");
  std::vector<CP2ProposalTraceFrame> frames;
  while (!input.empty()) {
    if (frames.size() >= limits.maximum_frames) {
      throw CP2TraceCodecError("proposal frame count exceeds configured limit");
    }
    CP2ProposalTraceFrame frame;
    const std::uint8_t role = input.ReadU8("proposal frame role");
    if (role > 1U) {
      throw CP2TraceCodecError("invalid proposal frame role");
    }
    frame.role = role == 0U ? CP2ProposalTraceRole::kNullspaceBaseline
                            : CP2ProposalTraceRole::kSchurCandidate;
    const std::uint8_t reserved[7] = {0U, 0U, 0U, 0U, 0U, 0U, 0U};
    input.Expect(reserved, sizeof(reserved), "proposal frame reserved bytes");
    frame.invocation.sequence_index = input.ReadU64("proposal frame sequence index");
    frame.invocation.pair_index = input.ReadU64("proposal frame pair index");
    frame.invocation.invocation_id = input.ReadU64("proposal frame invocation ID");
    const std::uint64_t payload_length = input.ReadU64("proposal frame payload length");
    frame.payload.offset = checked_u64(input.position(), "proposal payload offset");
    frame.payload.length = payload_length;
    const std::vector<std::uint8_t> payload = input.ReadBytes(payload_length, "proposal frame payload");
    frame.payload.sha256 = sha256_hex(payload);
    frame.proposal =
        decode_proposal_payload(payload, limits, total_coefficients);
    frames.push_back(std::move(frame));
  }
  validate_proposal_frame_sequence(frames);
  return frames;
}

CP2AcceptedFeatureDigests
CP2TraceCodec::AcceptedFeatureDigests(const std::vector<std::uint64_t> &accepted_ids) {
  CP2AcceptedFeatureDigests result;
  result.processing_sequence = accepted_ids;
  result.sorted_set = accepted_ids;
  std::sort(result.sorted_set.begin(), result.sorted_set.end());
  if (std::adjacent_find(result.sorted_set.begin(), result.sorted_set.end()) !=
      result.sorted_set.end()) {
    throw CP2TraceCodecError("accepted feature sequence contains duplicate IDs");
  }

  CP2CanonicalBuffer set_payload;
  append_domain(set_payload, kAcceptedSetDomain, sizeof(kAcceptedSetDomain));
  set_payload.AppendU64(checked_u64(result.sorted_set.size(), "accepted set count"));
  for (std::uint64_t feature_id : result.sorted_set) {
    set_payload.AppendU64(feature_id);
  }
  result.set_sha256 = set_payload.Sha256Hex();

  CP2CanonicalBuffer sequence_payload;
  append_domain(sequence_payload, kAcceptedSequenceDomain,
                sizeof(kAcceptedSequenceDomain));
  sequence_payload.AppendU64(
      checked_u64(result.processing_sequence.size(), "accepted sequence count"));
  for (std::uint64_t feature_id : result.processing_sequence) {
    sequence_payload.AppendU64(feature_id);
  }
  result.sequence_sha256 = sequence_payload.Sha256Hex();
  return result;
}

CP2TraceReplayResult
CP2TraceCodec::ReplayInvocation(const CP2TraceReplayInput &input) {
  if (input.raw_frames.empty()) {
    throw CP2TraceCodecError("offline replay requires a nonzero raw-system invocation");
  }
  const std::vector<CP2RawSystemTraceFrame> decoded_raw_frames =
      DecodeRawSystemFile(input.raw_system_file_bytes);
  const std::vector<CP2ProposalTraceFrame> decoded_proposal_frames =
      DecodeProposalFile(input.proposal_file_bytes);

  std::vector<const CP2RawSystemTraceFrame *> retained_raw_frames;
  for (const CP2RawSystemTraceFrame &frame : decoded_raw_frames) {
    if (frame.invocation == input.invocation) {
      retained_raw_frames.push_back(&frame);
    }
  }
  std::vector<const CP2ProposalTraceFrame *> retained_proposal_frames;
  for (const CP2ProposalTraceFrame &frame : decoded_proposal_frames) {
    if (frame.invocation == input.invocation) {
      retained_proposal_frames.push_back(&frame);
    }
  }
  if (retained_raw_frames.size() != input.raw_frames.size() ||
      retained_proposal_frames.size() != input.proposal_frames.size()) {
    throw CP2TraceCodecError("replay frame population does not match retained file context");
  }

  validate_raw_frame_sequence(input.raw_frames);
  validate_proposal_frame_sequence(input.proposal_frames);
  validate_replay_prior_and_layout(input);

  CP2TraceReplayResult result;
  result.invocation = input.invocation;
  result.raw_system_sha256.reserve(input.raw_frames.size());
  for (std::size_t index = 0; index < input.raw_frames.size(); ++index) {
    const CP2RawSystemTraceFrame &frame = input.raw_frames[index];
    const CP2RawSystemTraceFrame &retained = *retained_raw_frames[index];
    if (frame.invocation != input.invocation || frame.feature_ordinal != index) {
      throw CP2TraceCodecError("raw frame is disconnected from replay invocation context");
    }
    if (frame.raw_system.H_x.rows() != retained.raw_system.H_x.rows() ||
        frame.raw_system.H_x.cols() != retained.raw_system.H_x.cols() ||
        frame.raw_system.H_f.rows() != retained.raw_system.H_f.rows() ||
        frame.raw_system.H_f.cols() != retained.raw_system.H_f.cols() ||
        frame.raw_system.residual.rows() != retained.raw_system.residual.rows() ||
        frame.raw_system.jacobian_layout.size() !=
            retained.raw_system.jacobian_layout.size()) {
      throw CP2TraceCodecError(
          "raw frame dimensions do not match retained bounded values");
    }
    const std::vector<std::uint8_t> payload = EncodeRawSystemPayload(frame.raw_system);
    const std::vector<std::uint8_t> retained_payload =
        EncodeRawSystemPayload(retained.raw_system);
    const std::string payload_sha256 = sha256_hex(payload);
    if (frame.invocation != retained.invocation ||
        frame.feature_ordinal != retained.feature_ordinal ||
        frame.raw_system.feature_id != retained.raw_system.feature_id ||
        frame.payload.offset != retained.payload.offset ||
        frame.payload.length != retained.payload.length ||
        frame.payload.sha256 != retained.payload.sha256 || payload != retained_payload) {
      throw CP2TraceCodecError("raw frame values/reference do not match retained file bytes");
    }
    validate_payload_reference(frame.payload, input.raw_system_file_bytes, payload, 80U,
                               "raw frame payload reference");
    result.raw_system_sha256.push_back(payload_sha256);
  }

  for (std::size_t index = 0; index < input.proposal_frames.size(); ++index) {
    const CP2ProposalTraceFrame &frame = input.proposal_frames[index];
    const CP2ProposalTraceFrame &retained = *retained_proposal_frames[index];
    if (frame.invocation != input.invocation || frame.invocation != retained.invocation ||
        frame.role != retained.role || frame.payload.offset != retained.payload.offset ||
        frame.payload.length != retained.payload.length ||
        frame.payload.sha256 != retained.payload.sha256) {
      throw CP2TraceCodecError(
          "proposal frame context/reference does not match retained file bytes");
    }
    if (frame.proposal.dx.rows() != retained.proposal.dx.rows() ||
        frame.proposal.P_plus.rows() != retained.proposal.P_plus.rows() ||
        frame.proposal.P_plus.cols() != retained.proposal.P_plus.cols()) {
      throw CP2TraceCodecError(
          "proposal frame dimensions do not match retained bounded values");
    }
    const std::vector<std::uint8_t> payload = EncodeProposalPayload(frame.proposal);
    const std::vector<std::uint8_t> retained_payload =
        EncodeProposalPayload(retained.proposal);
    if (payload != retained_payload) {
      throw CP2TraceCodecError("proposal frame values do not match retained file bytes");
    }
    validate_payload_reference(frame.payload, input.proposal_file_bytes, payload, 72U,
                               "proposal frame payload reference");
  }

  // Only after every retained-file and joined-frame check succeeds do we copy
  // the bounded prior, table, and raw systems into the strict math boundary.
  CP2ShadowMathInput math_input;
  math_input.prior = input.prior;
  math_input.sigma_px = input.sigma_px;
  math_input.sigma_px_sq = input.sigma_px_sq;
  math_input.chi2_multiplier = input.chi2_multiplier;
  math_input.chi_squared_table = input.chi_squared_table;
  math_input.raw_systems.reserve(retained_raw_frames.size());
  for (const CP2RawSystemTraceFrame *frame : retained_raw_frames) {
    math_input.raw_systems.push_back(frame->raw_system);
  }

  result.math = CP2ShadowMath::Process(std::move(math_input));
  // Do not fold per-mode assembly success into structural replay
  // completeness. With the bounded decoder, exact prior-block layouts, and
  // shape-preserving reducers above, a complete valid-layout input proves
  // both assembler appends are representable. The separate flags remain
  // evidence outcomes so a future defensive per-mode failure is retained,
  // with proposal presence bound independently below, rather than mislabeled
  // as truncated or corrupt trace input.
  if (!result.math.traversal_complete || !result.math.raw_layouts_valid ||
      result.math.duplicate_feature_id ||
      result.math.features.size() != input.raw_frames.size()) {
    throw CP2TraceCodecError(
        "decoded invocation failed the complete value-only replay contract");
  }
  result.baseline_accepted = AcceptedFeatureDigests(result.math.nullspace.accepted_ids);
  result.candidate_accepted = AcceptedFeatureDigests(result.math.schur.accepted_ids);

  const auto retained_role = [&retained_proposal_frames](CP2ProposalTraceRole role)
      -> const CP2ProposalTraceFrame * {
    for (const CP2ProposalTraceFrame *frame : retained_proposal_frames) {
      if (frame->role == role) {
        return frame;
      }
    }
    return nullptr;
  };
  const auto verify_derived_proposal = [&retained_role](
                                           bool available,
                                           const MSCKFUpdatePreviewResult &proposal,
                                           CP2ProposalTraceRole role,
                                           const char *what) {
    const CP2ProposalTraceFrame *retained = retained_role(role);
    if (available != (retained != nullptr)) {
      throw CP2TraceCodecError(std::string(what) + " proposal presence mismatch");
    }
    if (!available) {
      return;
    }
    CP2ProposalTracePayload derived;
    derived.dx = proposal.dx;
    derived.P_plus = proposal.P_plus;
    if (EncodeProposalPayload(derived) != EncodeProposalPayload(retained->proposal)) {
      throw CP2TraceCodecError(std::string(what) +
                               " proposal bytes are disconnected from replay math");
    }
  };
  verify_derived_proposal(result.math.nullspace.proposal_available,
                          result.math.nullspace.proposal,
                          CP2ProposalTraceRole::kNullspaceBaseline, "baseline");
  verify_derived_proposal(result.math.schur.proposal_available,
                          result.math.schur.proposal,
                          CP2ProposalTraceRole::kSchurCandidate, "candidate");
  return result;
}

} // namespace ov_msckf
