/*
 * SchurVIO-Lite CP2 strict runtime-context parser.
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#ifndef OV_MSCKF_CP2_RUNTIME_CONTEXT_H
#define OV_MSCKF_CP2_RUNTIME_CONTEXT_H

#include <cstdint>
#include <string>

namespace ov_msckf {

enum class CP2RuntimeTraceLevel : std::uint8_t {
  kRecordedFull,
  kSequence,
  kTiming,
};

const char *cp2_runtime_trace_level_name(CP2RuntimeTraceLevel level) noexcept;

struct CP2NullablePath {
  bool available = false;
  std::string value;
};

/** Exact value-only contents of one frozen ``cp2_runtime_context`` object. */
struct CP2RuntimeContext {
  std::string checkpoint;
  std::string run_id;
  std::uint64_t sequence_index = 0U;
  std::string sequence_id;
  std::string mode;
  bool shadow_enabled = false;
  CP2RuntimeTraceLevel trace_level = CP2RuntimeTraceLevel::kRecordedFull;
  std::string source_commit;
  std::string config_sha256;
  std::string bag_sha256;
  std::string resolved_parameters_sha256;
  std::string trace_directory;
  CP2NullablePath serial_trace_path;
  CP2NullablePath callback_trace_path;
  CP2NullablePath trajectory_trace_path;
  CP2NullablePath updater_trace_path;
  CP2NullablePath state_payload_path;
  CP2NullablePath proposal_payload_path;
  CP2NullablePath raw_system_payload_path;
  CP2NullablePath timing_trace_path;
  CP2NullablePath runtime_parameters_path;
  CP2NullablePath loader_map_before_path;
  CP2NullablePath loader_map_after_path;
  CP2NullablePath legacy_state_path;
  CP2NullablePath legacy_deviation_path;
  CP2NullablePath legacy_timing_path;
  std::string document_sha256;
};

/** Values supplied independently by the frozen launch/runner boundary. */
struct CP2RuntimeContextExpectation {
  std::string checkpoint;
  std::uint64_t sequence_index = 0U;
  std::string sequence_id;
  std::string mode;
  bool shadow_enabled = false;
  std::string trace_level;
  std::string trace_directory;
};

enum class CP2RuntimeContextStatus : std::uint8_t {
  kAccepted,
  kInvalidJson,
  kDuplicateKey,
  kWrongKeyInventory,
  kWrongType,
  kInvalidValue,
  kExpectationMismatch,
  kInvalidPath,
  kInvalidOutputCombination,
  kFileOpenFailure,
  kFileIdentityFailure,
  kFileTooLarge,
  kFileReadFailure,
  kAllocationFailure,
};

const char *cp2_runtime_context_status_name(CP2RuntimeContextStatus status) noexcept;

struct CP2RuntimeContextResult {
  CP2RuntimeContextStatus status = CP2RuntimeContextStatus::kInvalidJson;
  CP2RuntimeContext context;

  bool accepted() const noexcept {
    return status == CP2RuntimeContextStatus::kAccepted;
  }
};

/**
 * Strictly parse and validate one context document without touching the file
 * system. JSON duplicate keys, extra keys, non-JSON constants and value-type
 * coercions are rejected. Output is failure-atomic.
 */
CP2RuntimeContextResult ParseCP2RuntimeContext(
    const std::string &document,
    const CP2RuntimeContextExpectation &expectation) noexcept;

/**
 * Open one normalized absolute context path without following symlinks,
 * require a regular single-link file, bind and recheck its complete stat
 * identity around a bounded read, then call the strict parser above.
 */
CP2RuntimeContextResult ReadCP2RuntimeContextFile(
    const std::string &path,
    const CP2RuntimeContextExpectation &expectation,
    std::uint64_t maximum_bytes = UINT64_C(1048576)) noexcept;

} // namespace ov_msckf

#endif // OV_MSCKF_CP2_RUNTIME_CONTEXT_H
