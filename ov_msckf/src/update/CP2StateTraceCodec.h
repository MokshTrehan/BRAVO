/*
 * SchurVIO-Lite CP2 composite-state canonical trace codec.
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#ifndef OV_MSCKF_CP2_STATE_TRACE_CODEC_H
#define OV_MSCKF_CP2_STATE_TRACE_CODEC_H

#include "CP2CompositeState.h"
#include "CP2TraceCodec.h"

#include <cstdint>
#include <vector>

namespace ov_msckf {

/// One owning state-snapshot frame in the frozen CP2 state payload file.
struct CP2StateTraceFrame {
  CP2InvocationTraceIdentity invocation;
  CP2StatePhase phase = CP2StatePhase::kPhase0Prior;
  CP2CompositeStateSnapshot snapshot;
  CP2TracePayloadReference payload;
};

struct CP2EncodedStateFile {
  std::vector<std::uint8_t> bytes;
  std::vector<CP2TracePayloadReference> payloads;
};

/**
 * Lossless canonical codec for the frozen CP2 composite-state payload.
 *
 * This layer validates framing, exact enum/string spellings and bounded,
 * syntactically complete decoding. Decode success is not an artifact-validity
 * verdict: semantic partitions, identities, shapes, finiteness, quaternions,
 * and the required phase-pair equalities are checked by the phase-aware
 * composite/oracle layer. Keeping those judgments separate is necessary so a
 * complete mismatching phase 1 and complete invalid phase 3 remain losslessly
 * representable with their original IEEE-754 binary64 bits.
 */
class CP2StateTraceCodec {
public:
  /// Encode using the prior domain for phases 0/1 and postcommit domain for 2/3.
  static std::vector<std::uint8_t>
  EncodeSnapshotPayload(const CP2CompositeStateSnapshot &snapshot);

  /// Decode one bounded canonical payload and bind it to the supplied phase.
  static CP2CompositeStateSnapshot
  DecodeSnapshotPayload(const std::vector<std::uint8_t> &payload,
                        CP2StatePhase phase,
                        const CP2TraceDecodeLimits &limits = CP2TraceDecodeLimits());

  /// Encode/decode the exact 32-byte file magic and legal phase populations.
  static CP2EncodedStateFile
  EncodeStateFile(const std::vector<CP2StateTraceFrame> &frames);

  static std::vector<CP2StateTraceFrame>
  DecodeStateFile(const std::vector<std::uint8_t> &file,
                  const CP2TraceDecodeLimits &limits = CP2TraceDecodeLimits());
};

} // namespace ov_msckf

#endif // OV_MSCKF_CP2_STATE_TRACE_CODEC_H
