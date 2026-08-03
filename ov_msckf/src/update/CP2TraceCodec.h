/*
 * SchurVIO-Lite CP2 canonical trace codec and value-only replay boundary.
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#ifndef OV_MSCKF_CP2_TRACE_CODEC_H
#define OV_MSCKF_CP2_TRACE_CODEC_H

#include "CP2ShadowMath.h"

#include <Eigen/Core>

#include <cstddef>
#include <cstdint>
#include <stdexcept>
#include <string>
#include <vector>

namespace ov_msckf {

/// Structural or canonical-encoding failure in untrusted CP2 trace bytes.
class CP2TraceCodecError : public std::runtime_error {
public:
  explicit CP2TraceCodecError(const std::string &message) : std::runtime_error(message) {}
};

/**
 * Explicit allocation limits for an offline trace decode.
 *
 * The defaults are deliberately finite. A caller may lower them for a test or
 * raise them only after validating the enclosing artifact size independently.
 */
struct CP2TraceDecodeLimits {
  std::uint64_t maximum_file_bytes = UINT64_C(2147483648);
  std::uint64_t maximum_payload_bytes = UINT64_C(536870912);
  std::uint64_t maximum_frames = UINT64_C(1000000);
  std::uint64_t maximum_matrix_coefficients = UINT64_C(67108864);
  std::uint64_t maximum_total_matrix_coefficients = UINT64_C(134217728);
  std::uint64_t maximum_vector_rows = UINT64_C(67108864);
  std::uint64_t maximum_layout_blocks = UINT64_C(1048576);
};

/// Context carried by every frozen raw/proposal frame header.
struct CP2InvocationTraceIdentity {
  std::uint64_t sequence_index = 0;
  std::uint64_t pair_index = 0;
  std::uint64_t invocation_id = 0;
};

bool operator==(const CP2InvocationTraceIdentity &left,
                const CP2InvocationTraceIdentity &right) noexcept;
bool operator!=(const CP2InvocationTraceIdentity &left,
                const CP2InvocationTraceIdentity &right) noexcept;

/// Byte location and canonical payload identity retained by JSON trace rows.
struct CP2TracePayloadReference {
  std::uint64_t offset = 0;
  std::uint64_t length = 0;
  std::string sha256;
};

/// Owning raw-system frame. No estimator object or borrowed pointer is held.
struct CP2RawSystemTraceFrame {
  CP2InvocationTraceIdentity invocation;
  std::uint64_t feature_ordinal = 0;
  CP2RawFeatureSystem raw_system;
  CP2TracePayloadReference payload;
};

enum class CP2ProposalTraceRole : std::uint8_t { kNullspaceBaseline = 0, kSchurCandidate = 1 };

/// Exact proposal payload represented independently of preview diagnostics.
struct CP2ProposalTracePayload {
  Eigen::VectorXd dx;
  Eigen::MatrixXd P_plus;
};

/// Owning proposal frame with the frozen role and invocation context.
struct CP2ProposalTraceFrame {
  CP2InvocationTraceIdentity invocation;
  CP2ProposalTraceRole role = CP2ProposalTraceRole::kNullspaceBaseline;
  CP2ProposalTracePayload proposal;
  CP2TracePayloadReference payload;
};

struct CP2EncodedRawSystemFile {
  std::vector<std::uint8_t> bytes;
  std::vector<CP2TracePayloadReference> payloads;
};

struct CP2EncodedProposalFile {
  std::vector<std::uint8_t> bytes;
  std::vector<CP2TracePayloadReference> payloads;
};

/// Both frozen accepted-list identities for one unique processing sequence.
struct CP2AcceptedFeatureDigests {
  std::vector<std::uint64_t> processing_sequence;
  std::vector<std::uint64_t> sorted_set;
  std::string set_sha256;
  std::string sequence_sha256;
};

/**
 * One fully owning input to offline raw-to-proposal replay.
 *
 * The prior is the value-only covariance projection decoded from phase 0 by
 * the full state-snapshot layer. Raw frames retain their frozen file context;
 * ReplayInvocation rejects any context or ordinal disconnect before calling
 * CP2ShadowMath.
 */
struct CP2TraceReplayInput {
  CP2InvocationTraceIdentity invocation;
  MSCKFUpdatePreviewSnapshot prior;
  /// Complete retained files, including their frozen 32-byte domains.
  std::vector<std::uint8_t> raw_system_file_bytes;
  std::vector<std::uint8_t> proposal_file_bytes;
  /// Exact JSON-joined decoded frames for this invocation. Replay re-decodes
  /// both files and requires these values and references to match exactly.
  std::vector<CP2RawSystemTraceFrame> raw_frames;
  std::vector<CP2ProposalTraceFrame> proposal_frames;
  double sigma_px = 0.0;
  double sigma_px_sq = 0.0;
  double chi2_multiplier = 0.0;
  CP2FeatureGate::ChiSquaredTable chi_squared_table;
};

/// Math output plus independently reconstructible accepted-list identities.
struct CP2TraceReplayResult {
  CP2InvocationTraceIdentity invocation;
  std::vector<std::string> raw_system_sha256;
  CP2ShadowMathResult math;
  CP2AcceptedFeatureDigests baseline_accepted;
  CP2AcceptedFeatureDigests candidate_accepted;
};

class CP2TraceCodec {
public:
  /// Frozen `SchurVIO-CP2-raw-system-v1\0` payload.
  static std::vector<std::uint8_t> EncodeRawSystemPayload(const CP2RawFeatureSystem &raw_system);
  static CP2RawFeatureSystem
  DecodeRawSystemPayload(const std::vector<std::uint8_t> &payload,
                         const CP2TraceDecodeLimits &limits = CP2TraceDecodeLimits());

  /// Frozen `SchurVIO-CP2-proposal-v1\0` payload.
  static std::vector<std::uint8_t> EncodeProposalPayload(const CP2ProposalTracePayload &proposal);
  static CP2ProposalTracePayload
  DecodeProposalPayload(const std::vector<std::uint8_t> &payload,
                        const CP2TraceDecodeLimits &limits = CP2TraceDecodeLimits());

  /// Frozen binary file formats, including exact 32-byte file domains.
  static CP2EncodedRawSystemFile EncodeRawSystemFile(const std::vector<CP2RawSystemTraceFrame> &frames);
  static std::vector<CP2RawSystemTraceFrame>
  DecodeRawSystemFile(const std::vector<std::uint8_t> &file,
                      const CP2TraceDecodeLimits &limits = CP2TraceDecodeLimits());

  static CP2EncodedProposalFile EncodeProposalFile(const std::vector<CP2ProposalTraceFrame> &frames);
  static std::vector<CP2ProposalTraceFrame>
  DecodeProposalFile(const std::vector<std::uint8_t> &file,
                     const CP2TraceDecodeLimits &limits = CP2TraceDecodeLimits());

  /// Frozen accepted-set and accepted-sequence digests. Duplicate IDs fail.
  static CP2AcceptedFeatureDigests AcceptedFeatureDigests(const std::vector<std::uint64_t> &accepted_ids);

  /**
   * Validate an owning offline invocation and run the sole CP2 math kernel.
   *
   * Unlike the live value-only CP2ShadowMath boundary, this offline entry
   * point requires complete retained raw/proposal file bytes and exact decoded
   * frame references. Detached live values with default references are never
   * accepted as artifact replay evidence.
   */
  static CP2TraceReplayResult ReplayInvocation(const CP2TraceReplayInput &input);

  /**
   * Replay using complete files that the caller decoded once with this codec.
   *
   * This is the campaign-scale equivalent of ReplayInvocation: canonical
   * payload references are still checked against input file bytes, while the
   * complete decoded populations avoid reparsing multi-gigabyte files for
   * every invocation. The two decoded vectors must be the unchanged direct
   * results of DecodeRawSystemFile/DecodeProposalFile for input's files.
   */
  static CP2TraceReplayResult ReplayDecodedInvocation(
      const CP2TraceReplayInput &input,
      const std::vector<CP2RawSystemTraceFrame> &decoded_raw_frames,
      const std::vector<CP2ProposalTraceFrame> &decoded_proposal_frames);
};

} // namespace ov_msckf

#endif // OV_MSCKF_CP2_TRACE_CODEC_H
