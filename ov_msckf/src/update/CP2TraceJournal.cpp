/*
 * SchurVIO-Lite CP2 authoritative updater event journal.
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include "CP2TraceJournal.h"

#include "CP2StateTraceCodec.h"
#include "CP2TraceCodec.h"

#include <algorithm>
#include <cerrno>
#include <cstring>
#include <fcntl.h>
#include <limits>
#include <new>
#include <set>
#include <sys/stat.h>
#include <unistd.h>
#include <utility>

namespace {

using ov_msckf::CP2RecordedUpdateEvent;
using ov_msckf::CP2TraceJournalFailure;

constexpr std::uint64_t kSectionEventCore = UINT64_C(1);
constexpr std::uint64_t kSectionBaselineAcceptedIds = UINT64_C(2);
constexpr std::uint64_t kSectionCandidateAcceptedIds = UINT64_C(3);
constexpr std::uint64_t kSectionGlobalShadow = UINT64_C(4);
constexpr std::uint64_t kSectionCommitOracle = UINT64_C(5);
constexpr std::uint64_t kSectionStateFileFragment = UINT64_C(6);
constexpr std::uint64_t kSectionRawFileFragment = UINT64_C(7);
constexpr std::uint64_t kSectionProposalFileFragment = UINT64_C(8);
constexpr std::uint64_t kSectionFeatureSummary = UINT64_C(10);
constexpr std::uint64_t kSectionStateFileHeader = UINT64_C(101);
constexpr std::uint64_t kSectionProposalFileHeader = UINT64_C(102);
constexpr std::uint64_t kSectionRawFileHeader = UINT64_C(103);
constexpr std::size_t kCanonicalFileHeaderSize = 32U;

bool checked_add(std::uint64_t left, std::uint64_t right,
                 std::uint64_t &output) noexcept {
  return ov_msckf::cp2_checked_add_u64(left, right, output);
}

bool size_to_u64(std::size_t value, std::uint64_t &output) noexcept {
  if (sizeof(std::size_t) > sizeof(std::uint64_t) &&
      value > static_cast<std::size_t>(UINT64_MAX)) {
    return false;
  }
  output = static_cast<std::uint64_t>(value);
  return true;
}

bool u64_to_size(std::uint64_t value, std::size_t &output) noexcept {
  if (sizeof(std::size_t) < sizeof(std::uint64_t) &&
      value > static_cast<std::uint64_t>(
                  std::numeric_limits<std::size_t>::max())) {
    return false;
  }
  output = static_cast<std::size_t>(value);
  return true;
}

bool signed_index_to_wire(Eigen::Index value,
                          std::uint64_t &output) noexcept {
  static_assert(sizeof(Eigen::Index) <= sizeof(std::int64_t),
                "Eigen::Index must fit the journal signed-integer wire slot");
  output = static_cast<std::uint64_t>(static_cast<std::int64_t>(value));
  return true;
}

bool count_index_to_wire(Eigen::Index value,
                         std::uint64_t &output) noexcept {
  return ov_msckf::cp2_checked_eigen_index_to_u64(value, output);
}

std::uint64_t binary64_bits(double value) noexcept {
  static_assert(sizeof(double) == sizeof(std::uint64_t) &&
                    std::numeric_limits<double>::is_iec559,
                "CP2 requires IEEE-754 binary64 doubles");
  std::uint64_t result = 0U;
  std::memcpy(&result, &value, sizeof(result));
  return result;
}

double binary64_from_bits(std::uint64_t value) noexcept {
  double result = 0.0;
  std::memcpy(&result, &value, sizeof(result));
  return result;
}

std::int64_t signed_wire_value(std::uint64_t value) noexcept {
  std::int64_t result = 0;
  std::memcpy(&result, &value, sizeof(result));
  return result;
}

class Encoder {
public:
  explicit Encoder(std::vector<std::uint8_t> &bytes) : bytes_(bytes) {}

  bool U64(std::uint64_t value) {
    if (!Grow(UINT64_C(8))) {
      return false;
    }
    for (unsigned index = 0U; index < 8U; ++index) {
      bytes_.push_back(static_cast<std::uint8_t>(
          value >> (56U - 8U * index)));
    }
    return true;
  }

  bool Boolean(bool value) { return U64(value ? UINT64_C(1) : UINT64_C(0)); }
  bool Binary64(double value) { return U64(binary64_bits(value)); }
  bool Size(std::size_t value) {
    std::uint64_t wire = 0U;
    return size_to_u64(value, wire) && U64(wire);
  }

  bool Bytes(const std::uint8_t *data, std::size_t size) {
    std::uint64_t wire_size = 0U;
    if (!size_to_u64(size, wire_size) || !Grow(wire_size)) {
      return false;
    }
    if (size != 0U) {
      bytes_.insert(bytes_.end(), data, data + size);
    }
    return true;
  }

  bool Bytes(const std::vector<std::uint8_t> &bytes) {
    return Bytes(bytes.data(), bytes.size());
  }

  bool Section(std::uint64_t tag,
               const std::vector<std::uint8_t> &body) {
    std::uint64_t body_size = 0U;
    return size_to_u64(body.size(), body_size) && U64(tag) && U64(body_size) &&
           Bytes(body);
  }

private:
  bool Grow(std::uint64_t amount) const noexcept {
    std::uint64_t current = 0U;
    std::uint64_t next = 0U;
    std::size_t next_size = 0U;
    return size_to_u64(bytes_.size(), current) &&
           checked_add(current, amount, next) &&
           u64_to_size(next, next_size) && next_size <= bytes_.max_size();
  }

  std::vector<std::uint8_t> &bytes_;
};

template <typename Enum>
std::uint64_t enum_wire(Enum value) noexcept {
  return static_cast<std::uint64_t>(value);
}

template <typename Enum>
bool valid_enum(Enum value, Enum maximum) noexcept {
  return enum_wire(value) <= enum_wire(maximum);
}

bool valid_preview_enums(
    const ov_msckf::MSCKFUpdatePreviewResult &preview) noexcept {
  return valid_enum(preview.diagnostics.status,
                    ov_msckf::MSCKFUpdatePreviewStatus::kNegativeDiagonal) &&
         valid_enum(preview.diagnostics.stage,
                    ov_msckf::MSCKFUpdatePreviewStage::kAccepted);
}

bool valid_comparison_enums(
    const ov_msckf::CP2StatisticComparison &comparison) noexcept {
  return valid_enum(
      comparison.status,
      ov_msckf::CP2StatisticComparisonStatus::kRatioNonfinite);
}

bool valid_feature_enums(
    const ov_msckf::CP2FeaturePairResult &feature) noexcept {
  return valid_enum(feature.nullspace.status,
                    ov_msckf::CP2NullspaceReductionStatus::kNonfinite) &&
         valid_enum(feature.nullspace.stage,
                    ov_msckf::CP2NullspaceReductionStage::kAccepted) &&
         valid_enum(feature.schur.status,
                    ov_msckf::SchurReductionStatus::kIllConditioned) &&
         valid_enum(feature.schur.stage,
                    ov_msckf::SchurReductionStage::kAccepted) &&
         valid_enum(feature.nullspace_gate.stage,
                    ov_msckf::CP2FeatureGateStage::kDecision) &&
         valid_enum(feature.schur_gate.stage,
                    ov_msckf::CP2FeatureGateStage::kDecision) &&
         valid_comparison_enums(feature.lambda_comparison) &&
         valid_comparison_enums(feature.eta_comparison) &&
         valid_comparison_enums(feature.gamma_comparison) &&
         valid_enum(feature.agreement_class,
                    ov_msckf::CP2GateAgreementClass::kNeitherDecision);
}

bool append_ids(const std::vector<std::uint64_t> &ids,
                std::vector<std::uint8_t> &body) {
  Encoder encoder(body);
  std::set<std::uint64_t> unique;
  for (std::uint64_t id : ids) {
    if (!unique.insert(id).second || !encoder.U64(id)) {
      return false;
    }
  }
  return true;
}

bool valid_terminal(const ov_msckf::CP2LiveUpdateEvent &update) noexcept {
  using Status = ov_msckf::CP2UpdateTerminalStatus;
  using Subreason = ov_msckf::CP2UpdateTerminalSubreason;
  switch (update.terminal_status) {
  case Status::kEmptyInput:
    return update.terminal_subreason == Subreason::kInputEmpty;
  case Status::kAllRejected:
    return update.terminal_subreason == Subreason::kNoFeaturesAfterCleaning ||
           update.terminal_subreason ==
               Subreason::kNoFeaturesAfterTriangulation ||
           update.terminal_subreason == Subreason::kNoRawSystems ||
           update.terminal_subreason ==
               Subreason::kAllBaselineFeaturesRejected;
  case Status::kEmptyAfterCompression:
    return update.terminal_subreason ==
           Subreason::kMeasurementCompressionEmpty;
  case Status::kPreflightRejected:
    return update.terminal_subreason ==
           Subreason::kBaselinePreflightRejected;
  case Status::kCommittedCounted:
    return update.terminal_subreason == Subreason::kNone;
  case Status::kInternalFailure:
    return update.terminal_subreason == Subreason::kInvalidLiveMode ||
           update.terminal_subreason == Subreason::kSnapshotMismatch ||
           update.terminal_subreason == Subreason::kTraceInvariantFailure;
  }
  return false;
}

std::uint64_t event_flags(const CP2RecordedUpdateEvent &record) noexcept {
  const ov_msckf::CP2LiveUpdateEvent &update = record.update;
  std::uint64_t flags = 0U;
  const auto bit = [&flags](unsigned index, bool value) {
    if (value) {
      flags |= UINT64_C(1) << index;
    }
  };
  bit(0U, update.baseline_precompression_system_nonempty);
  bit(1U, update.baseline_precompression_rows_available);
  bit(2U, update.baseline_compressed_system_nonempty);
  bit(3U, update.baseline_compressed_rows_available);
  bit(4U, update.baseline_preflight_attempted);
  bit(5U, update.baseline_preflight_accepted);
  bit(6U, update.baseline_commit_occurred);
  bit(7U, update.shadow_evidence_available);
  bit(8U, record.phase01_canonical_equal);
  bit(9U, record.phase01_pointer_graph_equal);
  bit(10U, record.baseline_proposal_payload_available);
  bit(11U, record.candidate_proposal_payload_available);
  bit(12U, record.baseline_commit_oracle_available);
  bit(13U, record.baseline_commit_mismatch);
  bit(14U, record.online_math_evidence_passed);
  bit(15U, update.shadow.shadow_math_completed);
  bit(16U, update.shadow.baseline_preview_available);
  bit(17U, update.shadow.baseline_commit_planned);
  bit(18U, update.shadow.result.traversal_complete);
  bit(19U, update.shadow.result.raw_layouts_valid);
  bit(20U, update.shadow.result.nullspace_assembly_valid);
  bit(21U, update.shadow.result.schur_assembly_valid);
  bit(22U, update.shadow.result.input_valid);
  bit(23U, update.shadow.result.duplicate_feature_id);
  bit(24U, update.shadow.result.nullspace.proposal_available);
  bit(25U, update.shadow.result.schur.proposal_available);
  return flags;
}

bool append_preview(const ov_msckf::MSCKFUpdatePreviewResult &preview,
                    Encoder &encoder) {
  const ov_msckf::MSCKFUpdatePreviewDiagnostics &value = preview.diagnostics;
  std::uint64_t state_dimension = 0U;
  std::uint64_t measurement_dimension = 0U;
  std::uint64_t jacobian_dimension = 0U;
  std::uint64_t ordered_dimension = 0U;
  std::uint64_t offending_order = 0U;
  std::uint64_t offending_diagonal = 0U;
  return count_index_to_wire(value.state_dimension, state_dimension) &&
         count_index_to_wire(value.measurement_dimension,
                             measurement_dimension) &&
         count_index_to_wire(value.jacobian_dimension, jacobian_dimension) &&
         count_index_to_wire(value.ordered_jacobian_dimension,
                             ordered_dimension) &&
         signed_index_to_wire(value.offending_order_index, offending_order) &&
         signed_index_to_wire(value.offending_diagonal_index,
                              offending_diagonal) &&
         encoder.U64(enum_wire(value.status)) &&
         encoder.U64(enum_wire(value.stage)) && encoder.U64(state_dimension) &&
         encoder.U64(measurement_dimension) &&
         encoder.U64(jacobian_dimension) &&
         encoder.U64(ordered_dimension) && encoder.U64(offending_order) &&
         encoder.U64(offending_diagonal) &&
         encoder.Boolean(value.minimum_posterior_diagonal_available) &&
         encoder.Binary64(value.minimum_posterior_diagonal) &&
         encoder.Size(value.jitter_count) && encoder.Size(value.repair_count) &&
         encoder.Size(value.alternate_solve_count) &&
         encoder.Size(value.clamp_count) &&
         encoder.Size(value.regularization_count) &&
         encoder.Size(value.fallback_count);
}

bool append_gate_counters(const ov_msckf::CP2FeatureGateCounters &value,
                          Encoder &encoder) {
  return encoder.Size(value.jitter_count) && encoder.Size(value.repair_count) &&
         encoder.Size(value.alternate_solve_count) &&
         encoder.Size(value.clamp_count) &&
         encoder.Size(value.regularization_count) &&
         encoder.Size(value.silent_fallback_count) &&
         encoder.Size(value.fallback_count);
}

bool append_gate(const ov_msckf::CP2FeatureGateResult &value,
                 Encoder &encoder) {
  std::uint64_t degrees = 0U;
  return count_index_to_wire(value.degrees_of_freedom, degrees) &&
         encoder.U64(enum_wire(value.stage)) && encoder.U64(degrees) &&
         encoder.Boolean(value.chi2_available) && encoder.Binary64(value.chi2) &&
         encoder.Boolean(value.threshold_available) &&
         encoder.Binary64(value.threshold) &&
         encoder.Boolean(value.evidence_decision_available) &&
         encoder.Boolean(value.evidence_accept) &&
         encoder.Boolean(value.lifecycle_accept) &&
         append_gate_counters(value.counters, encoder);
}

bool append_comparison(const ov_msckf::CP2StatisticComparison &value,
                       Encoder &encoder) {
  return encoder.U64(enum_wire(value.status)) &&
         encoder.Boolean(value.available) &&
         encoder.Binary64(value.reference_norm) &&
         encoder.Binary64(value.error) && encoder.Binary64(value.tolerance) &&
         encoder.Binary64(value.ratio) && encoder.Boolean(value.passed);
}

bool append_feature(const ov_msckf::CP2FeaturePairResult &feature,
                    std::vector<std::uint8_t> &body) {
  Encoder encoder(body);
  std::uint64_t raw_rows = 0U;
  std::uint64_t nullspace_raw_rows = 0U;
  std::uint64_t nullspace_reduced_rows = 0U;
  std::uint64_t schur_raw_rows = 0U;
  std::uint64_t schur_degrees = 0U;
  if (!count_index_to_wire(feature.raw_rows, raw_rows) ||
      !count_index_to_wire(feature.nullspace.raw_rows, nullspace_raw_rows) ||
      !count_index_to_wire(feature.nullspace.reduced_rows,
                           nullspace_reduced_rows) ||
      !count_index_to_wire(feature.schur.raw_rows, schur_raw_rows) ||
      !count_index_to_wire(feature.schur.degrees_of_freedom, schur_degrees)) {
    return false;
  }
  if (!encoder.U64(feature.feature_id) || !encoder.U64(raw_rows) ||
      !encoder.Boolean(feature.raw_layout_valid) ||
      !encoder.U64(enum_wire(feature.nullspace.status)) ||
      !encoder.U64(enum_wire(feature.nullspace.stage)) ||
      !encoder.U64(nullspace_raw_rows) ||
      !encoder.U64(nullspace_reduced_rows) ||
      !encoder.Boolean(feature.nullspace.emitted_system_available) ||
      !encoder.Boolean(feature.nullspace.mode_gamma_available) ||
      !encoder.Binary64(feature.nullspace.mode_gamma) ||
      !encoder.Boolean(
          feature.nullspace.statistics.raw_lambda_symmetry_error_available) ||
      !encoder.Boolean(feature.nullspace.statistics.lambda_available) ||
      !encoder.Boolean(feature.nullspace.statistics.eta_available) ||
      !encoder.Boolean(feature.nullspace.statistics.gamma_available) ||
      !encoder.Binary64(
          feature.nullspace.statistics.raw_lambda_symmetry_error_inf) ||
      !encoder.Binary64(feature.nullspace.statistics.gamma) ||
      !append_gate_counters(feature.nullspace.counters, encoder) ||
      !encoder.U64(enum_wire(feature.schur.status)) ||
      !encoder.U64(enum_wire(feature.schur.stage)) ||
      !encoder.U64(schur_raw_rows) || !encoder.U64(schur_degrees) ||
      !encoder.Boolean(feature.schur.singular_values_available) ||
      !encoder.Boolean(feature.schur.singular_ratio_available)) {
    return false;
  }
  for (Eigen::Index index = 0; index < 3; ++index) {
    if (!encoder.Binary64(feature.schur.singular_values(index))) {
      return false;
    }
  }
  return encoder.Binary64(feature.schur.singular_ratio) &&
         encoder.Binary64(feature.schur.raw_lambda_symmetry_error_inf) &&
         encoder.Binary64(feature.schur.gamma) &&
         encoder.Size(feature.schur.jitter_count) && encoder.U64(UINT64_C(0)) &&
         encoder.U64(UINT64_C(0)) && encoder.Size(feature.schur.clamp_count) &&
         encoder.Size(feature.schur.regularization_count) &&
         encoder.U64(UINT64_C(0)) && encoder.Size(feature.schur.fallback_count) &&
         append_gate(feature.nullspace_gate, encoder) &&
         append_gate(feature.schur_gate, encoder) &&
         encoder.Boolean(feature.statistics_comparison_required) &&
         append_comparison(feature.lambda_comparison, encoder) &&
         append_comparison(feature.eta_comparison, encoder) &&
         append_comparison(feature.gamma_comparison, encoder) &&
         encoder.U64(enum_wire(feature.agreement_class)) &&
         encoder.U64(feature.raw_row_match_weight);
}

bool exact_payload(const std::vector<std::uint8_t> &file,
                   const ov_msckf::CP2TracePayloadReference &reference,
                   const std::vector<std::uint8_t> &expected) {
  std::uint64_t file_size = 0U;
  std::uint64_t end = 0U;
  std::uint64_t expected_size = 0U;
  std::size_t offset = 0U;
  if (!size_to_u64(file.size(), file_size) ||
      !size_to_u64(expected.size(), expected_size) ||
      !checked_add(reference.offset, reference.length, end) ||
      end > file_size || reference.length != expected_size ||
      !u64_to_size(reference.offset, offset)) {
    return false;
  }
  return std::equal(expected.begin(), expected.end(), file.begin() + offset);
}

bool strip_header(const std::vector<std::uint8_t> &file,
                  std::vector<std::uint8_t> &fragment) {
  if (file.size() < kCanonicalFileHeaderSize) {
    return false;
  }
  fragment.assign(file.begin() + kCanonicalFileHeaderSize, file.end());
  return true;
}

class FileWriter final : public ov_msckf::CP2TraceJournalWriter {
public:
  explicit FileWriter(int descriptor) noexcept : descriptor_(descriptor) {}
  ~FileWriter() override {
    if (descriptor_ >= 0) {
      ::close(descriptor_);
    }
  }

  ov_msckf::CP2TraceJournalWriteResult
  Write(const std::uint8_t *data, std::size_t size) noexcept override {
    struct stat status {};
    const off_t current = ::lseek(descriptor_, 0, SEEK_CUR);
    if (descriptor_ < 0 || ::fstat(descriptor_, &status) != 0 ||
        !S_ISREG(status.st_mode) || status.st_nlink != 1 ||
        status.st_size < 0 || current < 0 ||
        static_cast<std::uint64_t>(status.st_size) != position_ ||
        static_cast<std::uint64_t>(current) != position_) {
      return {false, 0U};
    }
    const std::size_t maximum = static_cast<std::size_t>(
        std::numeric_limits<ssize_t>::max());
    const std::size_t request = std::min(size, maximum);
    ssize_t written = -1;
    do {
      written = ::write(descriptor_, data, request);
    } while (written < 0 && errno == EINTR);
    if (written < 0) {
      return {false, 0U};
    }
    const std::size_t committed = static_cast<std::size_t>(written);
    std::uint64_t committed_wire = 0U;
    std::uint64_t next = 0U;
    if (!size_to_u64(committed, committed_wire) ||
        !checked_add(position_, committed_wire, next)) {
      return {false, committed};
    }
    position_ = next;
    return {true, committed};
  }

  bool Sync(std::uint64_t expected_size) noexcept override {
    struct stat before {};
    const off_t current = ::lseek(descriptor_, 0, SEEK_CUR);
    if (descriptor_ < 0 || expected_size != position_ || current < 0 ||
        static_cast<std::uint64_t>(current) != position_ ||
        ::fstat(descriptor_, &before) != 0 || !S_ISREG(before.st_mode) ||
        before.st_nlink != 1 || before.st_size < 0 ||
        static_cast<std::uint64_t>(before.st_size) != position_) {
      return false;
    }
    int sync_status = -1;
    do {
      sync_status = ::fsync(descriptor_);
    } while (sync_status != 0 && errno == EINTR);
    if (sync_status != 0) {
      return false;
    }
    struct stat after {};
    const off_t final_offset = ::lseek(descriptor_, 0, SEEK_CUR);
    return final_offset >= 0 &&
           static_cast<std::uint64_t>(final_offset) == position_ &&
           ::fstat(descriptor_, &after) == 0 && S_ISREG(after.st_mode) &&
           after.st_nlink == 1 && after.st_size >= 0 &&
           static_cast<std::uint64_t>(after.st_size) == position_ &&
           before.st_dev == after.st_dev && before.st_ino == after.st_ino;
  }

private:
  int descriptor_ = -1;
  std::uint64_t position_ = 0U;
};

struct WireSection {
  std::uint64_t tag = 0U;
  std::vector<std::uint8_t> bytes;
};

struct WireFrame {
  std::uint64_t version = 0U;
  std::vector<WireSection> sections;
};

bool read_u64(const std::vector<std::uint8_t> &bytes, std::size_t &position,
              std::size_t end, std::uint64_t &value) noexcept {
  if (position > end || end - position < 8U) {
    return false;
  }
  value = 0U;
  for (unsigned index = 0U; index < 8U; ++index) {
    value = (value << 8U) | bytes[position + index];
  }
  position += 8U;
  return true;
}

bool section_word(const WireSection &section, std::size_t index,
                  std::uint64_t &value) noexcept {
  if (index > std::numeric_limits<std::size_t>::max() / 8U) {
    return false;
  }
  std::size_t position = index * 8U;
  return read_u64(section.bytes, position, section.bytes.size(), value);
}

bool parse_wire_frame(
    const std::vector<std::uint8_t> &bytes, std::size_t &position,
    const ov_msckf::CP2TraceJournalDecodeLimits &limits, WireFrame &frame,
    ov_msckf::CP2TraceJournalDecodeStatus &status) {
  using DecodeStatus = ov_msckf::CP2TraceJournalDecodeStatus;
  if (position > bytes.size()) {
    status = DecodeStatus::kArithmeticOverflow;
    return false;
  }
  if (bytes.size() - position < 8U) {
    status = position == bytes.size() ? DecodeStatus::kTruncatedFrame
                                      : DecodeStatus::kTrailingBytes;
    return false;
  }
  std::uint64_t body_length = 0U;
  if (!read_u64(bytes, position, bytes.size(), body_length)) {
    status = DecodeStatus::kTruncatedFrame;
    return false;
  }
  std::size_t body_size = 0U;
  if (!u64_to_size(body_length, body_size)) {
    status = DecodeStatus::kArithmeticOverflow;
    return false;
  }
  if (body_size > bytes.size() - position) {
    status = DecodeStatus::kTruncatedFrame;
    return false;
  }
  const std::size_t body_end = position + body_size;
  std::uint64_t section_count = 0U;
  if (!read_u64(bytes, position, body_end, frame.version) ||
      !read_u64(bytes, position, body_end, section_count)) {
    status = DecodeStatus::kTruncatedFrame;
    return false;
  }
  if (section_count > limits.maximum_sections_per_frame) {
    status = DecodeStatus::kSectionLimit;
    return false;
  }
  if (section_count >
      static_cast<std::uint64_t>((body_end - position) / 16U)) {
    status = DecodeStatus::kTruncatedFrame;
    return false;
  }
  std::size_t retained_section_count = 0U;
  if (!u64_to_size(section_count, retained_section_count)) {
    status = DecodeStatus::kArithmeticOverflow;
    return false;
  }
  frame.sections.clear();
  frame.sections.reserve(retained_section_count);
  for (std::size_t index = 0U; index < retained_section_count; ++index) {
    WireSection section;
    std::uint64_t section_length = 0U;
    if (!read_u64(bytes, position, body_end, section.tag) ||
        !read_u64(bytes, position, body_end, section_length)) {
      status = DecodeStatus::kTruncatedFrame;
      return false;
    }
    if (section_length > limits.maximum_section_bytes) {
      status = DecodeStatus::kSectionBytesLimit;
      return false;
    }
    std::size_t section_size = 0U;
    if (!u64_to_size(section_length, section_size)) {
      status = DecodeStatus::kArithmeticOverflow;
      return false;
    }
    if (section_size > body_end - position) {
      status = DecodeStatus::kTruncatedFrame;
      return false;
    }
    section.bytes.assign(bytes.begin() + static_cast<std::ptrdiff_t>(position),
                         bytes.begin() + static_cast<std::ptrdiff_t>(
                                             position + section_size));
    position += section_size;
    frame.sections.push_back(std::move(section));
  }
  if (position != body_end) {
    status = DecodeStatus::kTrailingBytes;
    return false;
  }
  return true;
}

bool exact_tag(const WireSection &section, std::uint64_t expected) noexcept {
  return section.tag == expected;
}

bool valid_boolean(std::uint64_t value) noexcept { return value <= 1U; }

bool validate_ids(const WireSection &section,
                  std::uint64_t maximum_count,
                  std::vector<std::uint64_t> &retained_ids) {
  if (section.bytes.size() % 8U != 0U ||
      section.bytes.size() / 8U > maximum_count) {
    return false;
  }
  retained_ids.clear();
  retained_ids.reserve(section.bytes.size() / 8U);
  std::set<std::uint64_t> ids;
  for (std::size_t index = 0U; index < section.bytes.size() / 8U; ++index) {
    std::uint64_t id = 0U;
    if (!section_word(section, index, id) || !ids.insert(id).second) {
      return false;
    }
    retained_ids.push_back(id);
  }
  return true;
}

bool ordered_subsequence(const std::vector<std::uint64_t> &selected,
                         const std::vector<std::uint64_t> &population) noexcept {
  std::size_t selected_index = 0U;
  for (std::uint64_t value : population) {
    if (selected_index < selected.size() &&
        selected[selected_index] == value) {
      ++selected_index;
    }
  }
  return selected_index == selected.size();
}

void parse_counter_words(
    const std::array<std::uint64_t, 98> &words, std::size_t base,
    ov_msckf::CP2TraceJournalCounterSummary &output) noexcept {
  output.jitter = words[base];
  output.repair = words[base + 1U];
  output.alternate_solve = words[base + 2U];
  output.clamp = words[base + 3U];
  output.regularization = words[base + 4U];
  output.silent_fallback = words[base + 5U];
  output.fallback = words[base + 6U];
}

bool parse_global_section(
    const WireSection &section,
    ov_msckf::CP2TraceJournalGlobalSummary &output,
    bool &nullspace_proposal_accepted,
    bool &candidate_proposal_accepted) noexcept {
  constexpr std::size_t kWordCount = 40U;
  if (section.bytes.size() != kWordCount * 8U) {
    return false;
  }
  std::array<std::uint64_t, kWordCount> words{};
  for (std::size_t index = 0U; index < words.size(); ++index) {
    if (!section_word(section, index, words[index])) {
      return false;
    }
  }
  if (words[0] > enum_wire(ov_msckf::CP2GammaStatus::kNonfinite) ||
      words[2] > enum_wire(ov_msckf::CP2GammaStatus::kNonfinite)) {
    return false;
  }
  for (std::size_t base : {std::size_t(8U), std::size_t(24U)}) {
    if (words[base] >
            enum_wire(ov_msckf::MSCKFUpdatePreviewStatus::kNegativeDiagonal) ||
        words[base + 1U] >
            enum_wire(ov_msckf::MSCKFUpdatePreviewStage::kAccepted) ||
        !valid_boolean(words[base + 8U])) {
      return false;
    }
  }
  nullspace_proposal_accepted =
      words[8] == enum_wire(ov_msckf::MSCKFUpdatePreviewStatus::kAccepted);
  candidate_proposal_accepted =
      words[24] == enum_wire(ov_msckf::MSCKFUpdatePreviewStatus::kAccepted);

  output.nullspace_gamma_status =
      static_cast<ov_msckf::CP2GammaStatus>(words[0]);
  output.nullspace_retained_gamma = binary64_from_bits(words[1]);
  output.schur_gamma_status =
      static_cast<ov_msckf::CP2GammaStatus>(words[2]);
  output.schur_retained_gamma = binary64_from_bits(words[3]);
  output.nullspace_precompression_rows = words[4];
  output.nullspace_compressed_rows = words[5];
  output.schur_precompression_rows = words[6];
  output.schur_compressed_rows = words[7];
  const auto parse_preview = [&words](
      std::size_t base,
      ov_msckf::CP2TraceJournalPreviewSummary &preview) noexcept {
    preview.status =
        static_cast<ov_msckf::MSCKFUpdatePreviewStatus>(words[base]);
    preview.stage =
        static_cast<ov_msckf::MSCKFUpdatePreviewStage>(words[base + 1U]);
    preview.state_dimension = words[base + 2U];
    preview.measurement_dimension = words[base + 3U];
    preview.jacobian_dimension = words[base + 4U];
    preview.ordered_jacobian_dimension = words[base + 5U];
    preview.offending_order_index = signed_wire_value(words[base + 6U]);
    preview.offending_diagonal_index = signed_wire_value(words[base + 7U]);
    preview.minimum_posterior_diagonal_available = words[base + 8U] != 0U;
    preview.minimum_posterior_diagonal =
        binary64_from_bits(words[base + 9U]);
    preview.counters.jitter = words[base + 10U];
    preview.counters.repair = words[base + 11U];
    preview.counters.alternate_solve = words[base + 12U];
    preview.counters.clamp = words[base + 13U];
    preview.counters.regularization = words[base + 14U];
    preview.counters.silent_fallback = 0U;
    preview.counters.fallback = words[base + 15U];
  };
  parse_preview(8U, output.nullspace_preview);
  parse_preview(24U, output.schur_preview);
  return true;
}

bool parse_oracle_section(
    const WireSection &section,
    ov_msckf::CP2CommitOracleResult &output) noexcept {
  if (section.bytes.size() != 12U * 8U) {
    return false;
  }
  std::uint64_t status = 0U;
  std::uint64_t flags = 0U;
  if (!section_word(section, 0U, status) ||
      !section_word(section, 1U, flags) ||
      status > enum_wire(ov_msckf::CP2CommitOracleStatus::kComplete) ||
      (flags & ~((UINT64_C(1) << 11U) - UINT64_C(1))) != 0U) {
    return false;
  }
  std::array<std::uint64_t, 10> counts{};
  for (std::size_t index = 0U; index < counts.size(); ++index) {
    if (!section_word(section, index + 2U, counts[index])) {
      return false;
    }
  }
  output.status = static_cast<ov_msckf::CP2CommitOracleStatus>(status);
  output.comparison_available = (flags & (UINT64_C(1) << 0U)) != 0U;
  output.structure_equal = (flags & (UINT64_C(1) << 1U)) != 0U;
  output.phase0_valid = (flags & (UINT64_C(1) << 2U)) != 0U;
  output.phase2_valid = (flags & (UINT64_C(1) << 3U)) != 0U;
  output.phase3_valid = (flags & (UINT64_C(1) << 4U)) != 0U;
  output.complete_finite = (flags & (UINT64_C(1) << 5U)) != 0U;
  output.phase0_phase2_immutable_equal =
      (flags & (UINT64_C(1) << 6U)) != 0U;
  output.phase2_phase3_canonical_equal =
      (flags & (UINT64_C(1) << 7U)) != 0U;
  output.complete_canonical_equal =
      (flags & (UINT64_C(1) << 8U)) != 0U;
  output.type_update_calls_match =
      (flags & (UINT64_C(1) << 9U)) != 0U;
  output.passed = (flags & (UINT64_C(1) << 10U)) != 0U;
  output.observed_type_update_calls = counts[0];
  output.baseline_expected_type_update_calls = counts[1];
  output.baseline_verified_nominal_fields = counts[2];
  output.baseline_nominal_mismatches = counts[3];
  output.baseline_covariance_mismatches = counts[4];
  output.baseline_fej_mismatches = counts[5];
  output.state_blocks_expected = counts[6];
  output.state_blocks_seen = counts[7];
  output.covariance_blocks_expected = counts[8];
  output.covariance_blocks_seen = counts[9];
  return true;
}

bool validate_gate_words(
    const std::array<std::uint64_t, 98> &words,
    std::size_t base) noexcept {
  return words[base] <=
             enum_wire(ov_msckf::CP2FeatureGateStage::kDecision) &&
         valid_boolean(words[base + 2U]) &&
         valid_boolean(words[base + 4U]) &&
         valid_boolean(words[base + 6U]) &&
         valid_boolean(words[base + 7U]) &&
         valid_boolean(words[base + 8U]);
}

bool validate_comparison_words(
    const std::array<std::uint64_t, 98> &words,
    std::size_t base) noexcept {
  return words[base] <= enum_wire(
                             ov_msckf::CP2StatisticComparisonStatus::
                                 kRatioNonfinite) &&
         valid_boolean(words[base + 1U]) &&
         valid_boolean(words[base + 6U]);
}

bool parse_feature_section(
    const WireSection &section,
    ov_msckf::CP2TraceJournalFeatureSummary &output) noexcept {
  constexpr std::size_t kWordCount = 98U;
  if (section.bytes.size() != kWordCount * 8U) {
    return false;
  }
  std::array<std::uint64_t, kWordCount> words{};
  for (std::size_t index = 0U; index < words.size(); ++index) {
    if (!section_word(section, index, words[index])) {
      return false;
    }
  }
  if (!(valid_boolean(words[2]) &&
         words[3] <=
             enum_wire(ov_msckf::CP2NullspaceReductionStatus::kNonfinite) &&
         words[4] <=
             enum_wire(ov_msckf::CP2NullspaceReductionStage::kAccepted) &&
         valid_boolean(words[7]) && valid_boolean(words[8]) &&
         valid_boolean(words[10]) && valid_boolean(words[11]) &&
         valid_boolean(words[12]) && valid_boolean(words[13]) &&
         words[23] <=
             enum_wire(ov_msckf::SchurReductionStatus::kIllConditioned) &&
         words[24] <=
             enum_wire(ov_msckf::SchurReductionStage::kAccepted) &&
         valid_boolean(words[27]) && valid_boolean(words[28]) &&
         words[36] == 0U && words[37] == 0U && words[40] == 0U &&
         validate_gate_words(words, 42U) &&
         validate_gate_words(words, 58U) && valid_boolean(words[74]) &&
         validate_comparison_words(words, 75U) &&
         validate_comparison_words(words, 82U) &&
         validate_comparison_words(words, 89U) &&
         words[96] <=
             enum_wire(ov_msckf::CP2GateAgreementClass::kNeitherDecision))) {
    return false;
  }

  output.feature_id = words[0];
  output.raw_rows = words[1];
  output.raw_layout_valid = words[2] != 0U;
  output.nullspace_status =
      static_cast<ov_msckf::CP2NullspaceReductionStatus>(words[3]);
  output.nullspace_stage =
      static_cast<ov_msckf::CP2NullspaceReductionStage>(words[4]);
  output.nullspace_raw_rows = words[5];
  output.nullspace_reduced_rows = words[6];
  output.nullspace_emitted_system_available = words[7] != 0U;
  output.nullspace_mode_gamma_available = words[8] != 0U;
  output.nullspace_mode_gamma = binary64_from_bits(words[9]);
  output.nullspace_raw_lambda_symmetry_error_available = words[10] != 0U;
  output.nullspace_lambda_available = words[11] != 0U;
  output.nullspace_eta_available = words[12] != 0U;
  output.nullspace_gamma_available = words[13] != 0U;
  output.nullspace_raw_lambda_symmetry_error_inf =
      binary64_from_bits(words[14]);
  output.nullspace_gamma = binary64_from_bits(words[15]);
  parse_counter_words(words, 16U, output.nullspace_reducer_counters);

  output.schur_status =
      static_cast<ov_msckf::SchurReductionStatus>(words[23]);
  output.schur_stage =
      static_cast<ov_msckf::SchurReductionStage>(words[24]);
  output.schur_raw_rows = words[25];
  output.schur_degrees_of_freedom = words[26];
  output.schur_singular_values_available = words[27] != 0U;
  output.schur_ratio_available = words[28] != 0U;
  for (std::size_t index = 0U; index < 3U; ++index) {
    output.schur_singular_values[index] = binary64_from_bits(words[29U + index]);
  }
  output.schur_ratio = binary64_from_bits(words[32]);
  output.schur_raw_lambda_symmetry_error_inf = binary64_from_bits(words[33]);
  output.schur_gamma = binary64_from_bits(words[34]);
  output.schur_reducer_counters.jitter = words[35];
  output.schur_reducer_counters.repair = words[36];
  output.schur_reducer_counters.alternate_solve = words[37];
  output.schur_reducer_counters.clamp = words[38];
  output.schur_reducer_counters.regularization = words[39];
  output.schur_reducer_counters.silent_fallback = words[40];
  output.schur_reducer_counters.fallback = words[41];

  const auto parse_gate = [&words](
      std::size_t base,
      ov_msckf::CP2TraceJournalGateSummary &gate) noexcept {
    gate.stage = static_cast<ov_msckf::CP2FeatureGateStage>(words[base]);
    gate.degrees_of_freedom = words[base + 1U];
    gate.chi2_available = words[base + 2U] != 0U;
    gate.chi2 = binary64_from_bits(words[base + 3U]);
    gate.threshold_available = words[base + 4U] != 0U;
    gate.threshold = binary64_from_bits(words[base + 5U]);
    gate.evidence_decision_available = words[base + 6U] != 0U;
    gate.evidence_accept = words[base + 7U] != 0U;
    gate.lifecycle_accept = words[base + 8U] != 0U;
    gate.counters.jitter = words[base + 9U];
    gate.counters.repair = words[base + 10U];
    gate.counters.alternate_solve = words[base + 11U];
    gate.counters.clamp = words[base + 12U];
    gate.counters.regularization = words[base + 13U];
    gate.counters.silent_fallback = words[base + 14U];
    gate.counters.fallback = words[base + 15U];
  };
  parse_gate(42U, output.nullspace_gate);
  parse_gate(58U, output.schur_gate);
  output.statistics_comparison_required = words[74] != 0U;
  const auto parse_comparison = [&words](
      std::size_t base,
      ov_msckf::CP2TraceJournalStatisticSummary &comparison) noexcept {
    comparison.status =
        static_cast<ov_msckf::CP2StatisticComparisonStatus>(words[base]);
    comparison.available = words[base + 1U] != 0U;
    comparison.reference_norm = binary64_from_bits(words[base + 2U]);
    comparison.error = binary64_from_bits(words[base + 3U]);
    comparison.tolerance = binary64_from_bits(words[base + 4U]);
    comparison.ratio = binary64_from_bits(words[base + 5U]);
    comparison.passed = words[base + 6U] != 0U;
  };
  parse_comparison(75U, output.lambda_comparison);
  parse_comparison(82U, output.eta_comparison);
  parse_comparison(89U, output.gamma_comparison);
  output.agreement_class =
      static_cast<ov_msckf::CP2GateAgreementClass>(words[96]);
  output.raw_row_match_weight = words[97];
  return true;
}

bool parse_event_core(const WireSection &section,
                      ov_msckf::CP2TraceJournalDecodedEvent &event,
                      bool &baseline_proposal,
                      bool &candidate_proposal) noexcept {
  constexpr std::size_t kWordCount = 25U;
  if (section.bytes.size() != kWordCount * 8U) {
    return false;
  }
  std::array<std::uint64_t, kWordCount> words{};
  for (std::size_t index = 0U; index < words.size(); ++index) {
    if (!section_word(section, index, words[index])) {
      return false;
    }
  }
  if (words[5] >
          enum_wire(ov_msckf::CP2UpdateTerminalStatus::kInternalFailure) ||
      words[6] > enum_wire(ov_msckf::CP2UpdateTerminalSubreason::kNone) ||
      words[10] > enum_wire(ov_msckf::CP2GammaStatus::kNonfinite) ||
      (words[9] & ~((UINT64_C(1) << 26U) - UINT64_C(1))) != 0U) {
    return false;
  }
  ov_msckf::CP2LiveUpdateEvent terminal;
  terminal.terminal_status =
      static_cast<ov_msckf::CP2UpdateTerminalStatus>(words[5]);
  terminal.terminal_subreason =
      static_cast<ov_msckf::CP2UpdateTerminalSubreason>(words[6]);
  if (!valid_terminal(terminal)) {
    return false;
  }

  const bool committed = terminal.terminal_status ==
                         ov_msckf::CP2UpdateTerminalStatus::kCommittedCounted;
  const bool zero_raw_terminal_valid =
      terminal.terminal_status ==
          ov_msckf::CP2UpdateTerminalStatus::kEmptyInput ||
      (terminal.terminal_status ==
           ov_msckf::CP2UpdateTerminalStatus::kAllRejected &&
       terminal.terminal_subreason !=
           ov_msckf::CP2UpdateTerminalSubreason::
               kAllBaselineFeaturesRejected) ||
      (terminal.terminal_status ==
           ov_msckf::CP2UpdateTerminalStatus::kInternalFailure &&
       terminal.terminal_subreason ==
           ov_msckf::CP2UpdateTerminalSubreason::kTraceInvariantFailure);
  const bool nonzero_raw_terminal_valid =
      (terminal.terminal_status ==
           ov_msckf::CP2UpdateTerminalStatus::kAllRejected &&
       terminal.terminal_subreason ==
           ov_msckf::CP2UpdateTerminalSubreason::
               kAllBaselineFeaturesRejected) ||
      terminal.terminal_status ==
          ov_msckf::CP2UpdateTerminalStatus::kEmptyAfterCompression ||
      terminal.terminal_status ==
          ov_msckf::CP2UpdateTerminalStatus::kPreflightRejected ||
      terminal.terminal_status ==
          ov_msckf::CP2UpdateTerminalStatus::kCommittedCounted ||
      terminal.terminal_status ==
          ov_msckf::CP2UpdateTerminalStatus::kInternalFailure;
  const std::uint64_t required_phases =
      words[8] == 0U ? 0U : (committed ? 4U : 2U);
  const bool baseline_commit_flag = (words[9] & (UINT64_C(1) << 6U)) != 0U;
  const bool oracle_available_flag =
      (words[9] & (UINT64_C(1) << 12U)) != 0U;
  baseline_proposal = (words[9] & (UINT64_C(1) << 10U)) != 0U;
  candidate_proposal = (words[9] & (UINT64_C(1) << 11U)) != 0U;
  const bool shadow_evidence =
      (words[9] & (UINT64_C(1) << 7U)) != 0U;
  const bool shadow_math_completed =
      (words[9] & (UINT64_C(1) << 15U)) != 0U;
  const bool baseline_preview_available =
      (words[9] & (UINT64_C(1) << 16U)) != 0U;
  const bool traversal_complete =
      (words[9] & (UINT64_C(1) << 18U)) != 0U;
  const std::uint64_t zero_raw_forbidden_flags =
      (UINT64_C(1) << 7U) |
      (((UINT64_C(1) << 26U) - UINT64_C(1)) &
       ~((UINT64_C(1) << 15U) - UINT64_C(1)));
  if (words[8] > words[7] || words[8] != words[22] ||
      words[8] != words[24] || words[23] != required_phases ||
      (words[8] == 0U ? !zero_raw_terminal_valid
                      : !nonzero_raw_terminal_valid) ||
      (terminal.terminal_status ==
           ov_msckf::CP2UpdateTerminalStatus::kEmptyInput &&
       words[7] != 0U) ||
      baseline_commit_flag != committed ||
      oracle_available_flag != committed ||
      words[14] != (committed ? 1U : 0U) ||
      words[15] != (committed ? 1U : 0U) ||
      words[16] != (committed ? 1U : 0U) ||
      (committed && !baseline_proposal) ||
      (baseline_proposal && !baseline_preview_available) ||
      (words[8] != 0U &&
       (!shadow_evidence || !shadow_math_completed || !traversal_complete)) ||
      (words[8] == 0U &&
       ((words[9] & zero_raw_forbidden_flags) != 0U || baseline_proposal ||
        candidate_proposal))) {
    return false;
  }
  const auto flag = [&words](unsigned bit) noexcept {
    return (words[9] & (UINT64_C(1) << bit)) != 0U;
  };
  event.core.duration_ns = words[4];
  event.core.terminal_status = terminal.terminal_status;
  event.core.terminal_subreason = terminal.terminal_subreason;
  event.core.input_feature_count = words[7];
  event.core.raw_system_count = words[8];
  event.core.baseline_precompression_system_nonempty = flag(0U);
  event.core.baseline_precompression_rows_available = flag(1U);
  event.core.baseline_compressed_system_nonempty = flag(2U);
  event.core.baseline_compressed_rows_available = flag(3U);
  event.core.baseline_preflight_attempted = flag(4U);
  event.core.baseline_preflight_accepted = flag(5U);
  event.core.baseline_commit_occurred = flag(6U);
  event.core.shadow_evidence_available = flag(7U);
  event.core.phase01_canonical_equal = flag(8U);
  event.core.phase01_pointer_graph_equal = flag(9U);
  event.core.baseline_proposal_payload_available = flag(10U);
  event.core.candidate_proposal_payload_available = flag(11U);
  event.core.baseline_commit_oracle_available = flag(12U);
  event.core.baseline_commit_mismatch = flag(13U);
  event.core.online_math_evidence_passed = flag(14U);
  event.core.shadow_math_completed = flag(15U);
  event.core.baseline_preview_available = flag(16U);
  event.core.baseline_commit_planned = flag(17U);
  event.core.traversal_complete = flag(18U);
  event.core.raw_layouts_valid = flag(19U);
  event.core.nullspace_assembly_valid = flag(20U);
  event.core.schur_assembly_valid = flag(21U);
  event.core.input_valid = flag(22U);
  event.core.duplicate_feature_id = flag(23U);
  event.core.nullspace_proposal_available = flag(24U);
  event.core.schur_proposal_available = flag(25U);
  event.core.baseline_gamma_status =
      static_cast<ov_msckf::CP2GammaStatus>(words[10]);
  event.core.baseline_gamma = binary64_from_bits(words[11]);
  event.core.baseline_precompression_rows = words[12];
  event.core.baseline_compressed_rows = words[13];
  event.core.baseline_commit_count = words[14];
  event.core.baseline_mean_commit_count = words[15];
  event.core.baseline_covariance_commit_count = words[16];
  event.core.candidate_ekf_update_call_count = words[17];
  event.core.candidate_mean_write_count = words[18];
  event.core.candidate_covariance_write_count = words[19];
  event.core.candidate_type_update_call_count = words[20];
  event.core.candidate_feature_write_count = words[21];
  event.core.raw_payload_count = words[22];
  event.core.state_phase_count = words[23];
  event.core.feature_summary_count = words[24];
  event.sequence_index = words[0];
  event.pair_index = words[1];
  event.camera_timestamp_ns = words[2];
  event.invocation_id = words[3];
  event.raw_system_count = words[8];
  event.state_phase_count = words[23];
  event.feature_summary_count = words[24];
  return true;
}

bool append_limited(std::vector<std::uint8_t> &destination,
                    const std::vector<std::uint8_t> &source,
                    std::uint64_t maximum) {
  std::uint64_t destination_size = 0U;
  std::uint64_t source_size = 0U;
  std::uint64_t final_size = 0U;
  if (!size_to_u64(destination.size(), destination_size) ||
      !size_to_u64(source.size(), source_size) ||
      !checked_add(destination_size, source_size, final_size) ||
      final_size > maximum) {
    return false;
  }
  destination.insert(destination.end(), source.begin(), source.end());
  return true;
}

bool same_identity(const ov_msckf::CP2InvocationTraceIdentity &identity,
                   const ov_msckf::CP2TraceJournalDecodedEvent &event) noexcept {
  return identity.sequence_index == event.sequence_index &&
         identity.pair_index == event.pair_index &&
         identity.invocation_id == event.invocation_id;
}

bool canonical_headers(std::vector<std::uint8_t> &state,
                       std::vector<std::uint8_t> &proposal,
                       std::vector<std::uint8_t> &raw) {
  state = ov_msckf::CP2StateTraceCodec::EncodeStateFile({}).bytes;
  proposal = ov_msckf::CP2TraceCodec::EncodeProposalFile({}).bytes;
  raw = ov_msckf::CP2TraceCodec::EncodeRawSystemFile({}).bytes;
  return state.size() == kCanonicalFileHeaderSize &&
         proposal.size() == kCanonicalFileHeaderSize &&
         raw.size() == kCanonicalFileHeaderSize;
}

bool exact_bootstrap(const WireFrame &frame,
                     const std::vector<std::uint8_t> &state_header,
                     const std::vector<std::uint8_t> &proposal_header,
                     const std::vector<std::uint8_t> &raw_header) noexcept {
  return frame.sections.size() == 3U &&
         exact_tag(frame.sections[0], kSectionStateFileHeader) &&
         exact_tag(frame.sections[1], kSectionProposalFileHeader) &&
         exact_tag(frame.sections[2], kSectionRawFileHeader) &&
         frame.sections[0].bytes == state_header &&
         frame.sections[1].bytes == proposal_header &&
         frame.sections[2].bytes == raw_header;
}

bool exact_codec_file(const std::vector<std::uint8_t> &bytes,
                      const std::vector<ov_msckf::CP2StateTraceFrame> &frames) {
  return ov_msckf::CP2StateTraceCodec::EncodeStateFile(frames).bytes == bytes;
}

bool exact_codec_file(
    const std::vector<std::uint8_t> &bytes,
    const std::vector<ov_msckf::CP2ProposalTraceFrame> &frames) {
  return ov_msckf::CP2TraceCodec::EncodeProposalFile(frames).bytes == bytes;
}

bool exact_codec_file(
    const std::vector<std::uint8_t> &bytes,
    const std::vector<ov_msckf::CP2RawSystemTraceFrame> &frames) {
  return ov_msckf::CP2TraceCodec::EncodeRawSystemFile(frames).bytes == bytes;
}

bool validate_event_identity(
    const ov_msckf::CP2TraceJournalDecodedEvent *previous,
    const ov_msckf::CP2TraceJournalDecodedEvent &event,
    ov_msckf::CP2TraceJournalDecodeStatus &status) noexcept {
  if (previous == nullptr) {
    if (event.invocation_id != 0U) {
      status = ov_msckf::CP2TraceJournalDecodeStatus::kIdentityViolation;
      return false;
    }
    return true;
  }
  std::uint64_t expected_invocation = 0U;
  if (!checked_add(previous->invocation_id, UINT64_C(1),
                   expected_invocation)) {
    status = ov_msckf::CP2TraceJournalDecodeStatus::kArithmeticOverflow;
    return false;
  }
  if (event.sequence_index != previous->sequence_index ||
      event.invocation_id != expected_invocation ||
      event.pair_index < previous->pair_index ||
      (event.pair_index == previous->pair_index &&
       event.camera_timestamp_ns != previous->camera_timestamp_ns)) {
    status = ov_msckf::CP2TraceJournalDecodeStatus::kIdentityViolation;
    return false;
  }
  return true;
}

bool append_frame_bytes(std::vector<std::uint8_t> &output,
                        const WireFrame &frame) {
  std::vector<std::uint8_t> body;
  Encoder body_encoder(body);
  std::uint64_t section_count = 0U;
  if (!size_to_u64(frame.sections.size(), section_count) ||
      !body_encoder.U64(frame.version) ||
      !body_encoder.U64(section_count)) {
    return false;
  }
  for (const WireSection &section : frame.sections) {
    if (!body_encoder.Section(section.tag, section.bytes)) {
      return false;
    }
  }
  Encoder output_encoder(output);
  std::uint64_t body_size = 0U;
  return size_to_u64(body.size(), body_size) && output_encoder.U64(body_size) &&
         output_encoder.Bytes(body);
}

} // namespace

constexpr std::uint64_t
    ov_msckf::CP2TraceJournalSink::kFormatVersion;
constexpr std::uint64_t
    ov_msckf::CP2TraceJournalSink::kDefaultMaximumJournalBytes;

const char *ov_msckf::cp2_trace_journal_failure_name(
    CP2TraceJournalFailure failure) noexcept {
  switch (failure) {
  case CP2TraceJournalFailure::kNone:
    return "none";
  case CP2TraceJournalFailure::kWriterUnavailable:
    return "writer_unavailable";
  case CP2TraceJournalFailure::kFileInvalid:
    return "file_invalid";
  case CP2TraceJournalFailure::kNullRecord:
    return "null_record";
  case CP2TraceJournalFailure::kIdentityViolation:
    return "identity_violation";
  case CP2TraceJournalFailure::kRecordInvariant:
    return "record_invariant";
  case CP2TraceJournalFailure::kArithmeticOverflow:
    return "arithmetic_overflow";
  case CP2TraceJournalFailure::kEncodingFailure:
    return "encoding_failure";
  case CP2TraceJournalFailure::kWriteFailure:
    return "write_failure";
  case CP2TraceJournalFailure::kSyncFailure:
    return "sync_failure";
  }
  return "unknown";
}

const char *ov_msckf::cp2_trace_journal_decode_status_name(
    CP2TraceJournalDecodeStatus status) noexcept {
  switch (status) {
  case CP2TraceJournalDecodeStatus::kAccepted:
    return "accepted";
  case CP2TraceJournalDecodeStatus::kTotalBytesLimit:
    return "total_bytes_limit";
  case CP2TraceJournalDecodeStatus::kInvalidHeader:
    return "invalid_header";
  case CP2TraceJournalDecodeStatus::kTrailingBytes:
    return "trailing_bytes";
  case CP2TraceJournalDecodeStatus::kTruncatedFrame:
    return "truncated_frame";
  case CP2TraceJournalDecodeStatus::kArithmeticOverflow:
    return "arithmetic_overflow";
  case CP2TraceJournalDecodeStatus::kFrameLimit:
    return "frame_limit";
  case CP2TraceJournalDecodeStatus::kSectionLimit:
    return "section_limit";
  case CP2TraceJournalDecodeStatus::kSectionBytesLimit:
    return "section_bytes_limit";
  case CP2TraceJournalDecodeStatus::kInvalidVersion:
    return "invalid_version";
  case CP2TraceJournalDecodeStatus::kInvalidBootstrap:
    return "invalid_bootstrap";
  case CP2TraceJournalDecodeStatus::kInvalidSectionInventory:
    return "invalid_section_inventory";
  case CP2TraceJournalDecodeStatus::kInvalidSectionValue:
    return "invalid_section_value";
  case CP2TraceJournalDecodeStatus::kIdentityViolation:
    return "identity_violation";
  case CP2TraceJournalDecodeStatus::kCanonicalCodecFailure:
    return "canonical_codec_failure";
  case CP2TraceJournalDecodeStatus::kAllocationFailure:
    return "allocation_failure";
  }
  return "unknown";
}

ov_msckf::CP2TraceJournalDecodeResult
ov_msckf::CP2TraceJournalCodec::Decode(
    const std::vector<std::uint8_t> &bytes,
    const CP2TraceJournalDecodeLimits &limits) noexcept {
  CP2TraceJournalDecodeResult result;
  try {
    std::uint64_t input_size = 0U;
    if (!size_to_u64(bytes.size(), input_size)) {
      result.status = CP2TraceJournalDecodeStatus::kArithmeticOverflow;
      return result;
    }
    if (input_size > limits.maximum_total_bytes) {
      result.status = CP2TraceJournalDecodeStatus::kTotalBytesLimit;
      return result;
    }
    const std::array<std::uint8_t, 32> &journal_header =
        CP2TraceJournalSink::FileHeader();
    if (bytes.size() < journal_header.size() ||
        !std::equal(journal_header.begin(), journal_header.end(),
                    bytes.begin())) {
      result.status = CP2TraceJournalDecodeStatus::kInvalidHeader;
      return result;
    }

    CP2TraceJournalDecoded decoded;
    std::vector<std::uint8_t> state_header;
    std::vector<std::uint8_t> proposal_header;
    std::vector<std::uint8_t> raw_header;
    if (!canonical_headers(state_header, proposal_header, raw_header)) {
      result.status = CP2TraceJournalDecodeStatus::kCanonicalCodecFailure;
      return result;
    }
    decoded.state_file_bytes = state_header;
    decoded.proposal_file_bytes = proposal_header;
    decoded.raw_system_file_bytes = raw_header;

    std::size_t position = journal_header.size();
    const std::size_t bootstrap_begin = position;
    WireFrame bootstrap;
    CP2TraceJournalDecodeStatus parse_status =
        CP2TraceJournalDecodeStatus::kAccepted;
    if (!parse_wire_frame(bytes, position, limits, bootstrap, parse_status)) {
      result.status = parse_status;
      return result;
    }
    if (bootstrap.version != CP2TraceJournalSink::kFormatVersion) {
      result.status = CP2TraceJournalDecodeStatus::kInvalidVersion;
      return result;
    }
    if (!exact_bootstrap(bootstrap, state_header, proposal_header,
                         raw_header)) {
      result.status = CP2TraceJournalDecodeStatus::kInvalidBootstrap;
      return result;
    }

    std::vector<std::uint8_t> encoded_frame;
    if (!append_frame_bytes(encoded_frame, bootstrap)) {
      result.status = CP2TraceJournalDecodeStatus::kArithmeticOverflow;
      return result;
    }
    if (encoded_frame.size() != position - bootstrap_begin ||
        !std::equal(encoded_frame.begin(), encoded_frame.end(),
                    bytes.begin() +
                        static_cast<std::ptrdiff_t>(bootstrap_begin))) {
      result.status = CP2TraceJournalDecodeStatus::kInvalidBootstrap;
      return result;
    }

    CP2TraceDecodeLimits codec_limits;
    codec_limits.maximum_file_bytes = limits.maximum_total_bytes;
    codec_limits.maximum_payload_bytes =
        std::min(limits.maximum_total_bytes, limits.maximum_section_bytes);
    codec_limits.maximum_frames =
        limits.maximum_total_bytes / UINT64_C(40) + UINT64_C(1);
    codec_limits.maximum_matrix_coefficients =
        limits.maximum_total_bytes / UINT64_C(8);
    codec_limits.maximum_total_matrix_coefficients =
        limits.maximum_total_bytes / UINT64_C(8);
    codec_limits.maximum_vector_rows =
        limits.maximum_total_bytes / UINT64_C(8);
    codec_limits.maximum_layout_blocks =
        limits.maximum_total_bytes / UINT64_C(16) + UINT64_C(1);

    std::uint64_t frame_count = 0U;
    while (position < bytes.size()) {
      if (frame_count >= limits.maximum_frames) {
        result.status = CP2TraceJournalDecodeStatus::kFrameLimit;
        return result;
      }
      const std::size_t frame_begin = position;
      WireFrame frame;
      parse_status = CP2TraceJournalDecodeStatus::kAccepted;
      if (!parse_wire_frame(bytes, position, limits, frame, parse_status)) {
        result.status = parse_status;
        return result;
      }
      if (frame.version != CP2TraceJournalSink::kFormatVersion) {
        result.status = CP2TraceJournalDecodeStatus::kInvalidVersion;
        return result;
      }
      if (frame.sections.size() < 8U ||
          !exact_tag(frame.sections[0], kSectionEventCore) ||
          !exact_tag(frame.sections[1], kSectionBaselineAcceptedIds) ||
          !exact_tag(frame.sections[2], kSectionCandidateAcceptedIds) ||
          !exact_tag(frame.sections[3], kSectionGlobalShadow) ||
          !exact_tag(frame.sections[4], kSectionCommitOracle) ||
          !exact_tag(frame.sections[5], kSectionStateFileFragment) ||
          !exact_tag(frame.sections[6], kSectionRawFileFragment) ||
          !exact_tag(frame.sections[7], kSectionProposalFileFragment)) {
        result.status =
            CP2TraceJournalDecodeStatus::kInvalidSectionInventory;
        return result;
      }

      CP2TraceJournalDecodedEvent event;
      bool baseline_proposal = false;
      bool candidate_proposal = false;
      if (!parse_event_core(frame.sections[0], event, baseline_proposal,
                            candidate_proposal)) {
        result.status = CP2TraceJournalDecodeStatus::kInvalidSectionValue;
        return result;
      }
      std::uint64_t expected_section_count = UINT64_C(8);
      std::uint64_t retained_section_count = 0U;
      if (!checked_add(expected_section_count, event.feature_summary_count,
                       expected_section_count) ||
          !size_to_u64(frame.sections.size(), retained_section_count)) {
        result.status = CP2TraceJournalDecodeStatus::kArithmeticOverflow;
        return result;
      }
      if (retained_section_count != expected_section_count) {
        result.status =
            CP2TraceJournalDecodeStatus::kInvalidSectionInventory;
        return result;
      }
      for (std::size_t index = 8U; index < frame.sections.size(); ++index) {
        if (!exact_tag(frame.sections[index], kSectionFeatureSummary)) {
          result.status =
              CP2TraceJournalDecodeStatus::kInvalidSectionInventory;
          return result;
        }
      }
      bool nullspace_proposal_accepted = false;
      bool candidate_proposal_accepted = false;
      std::vector<std::uint64_t> baseline_ids;
      std::vector<std::uint64_t> candidate_ids;
      if (!validate_ids(frame.sections[1], event.raw_system_count,
                        baseline_ids) ||
          !validate_ids(frame.sections[2], event.raw_system_count,
                        candidate_ids) ||
          !parse_global_section(frame.sections[3], event.global,
                                nullspace_proposal_accepted,
                                candidate_proposal_accepted) ||
          !parse_oracle_section(frame.sections[4], event.commit_oracle)) {
        result.status = CP2TraceJournalDecodeStatus::kInvalidSectionValue;
        return result;
      }
      std::uint64_t core_flags = 0U;
      if (!section_word(frame.sections[0], 9U, core_flags) ||
          (((core_flags & (UINT64_C(1) << 24U)) != 0U) !=
           nullspace_proposal_accepted) ||
          (((core_flags & (UINT64_C(1) << 25U)) != 0U) !=
           candidate_proposal_accepted) ||
          candidate_proposal !=
              ((core_flags & (UINT64_C(1) << 25U)) != 0U)) {
        result.status = CP2TraceJournalDecodeStatus::kInvalidSectionValue;
        return result;
      }
      if (!validate_event_identity(
              decoded.events.empty() ? nullptr : &decoded.events.back(),
              event, parse_status)) {
        result.status = parse_status;
        return result;
      }

      std::vector<std::uint64_t> feature_ids;
      std::size_t feature_count = 0U;
      if (!u64_to_size(event.feature_summary_count, feature_count)) {
        result.status = CP2TraceJournalDecodeStatus::kArithmeticOverflow;
        return result;
      }
      feature_ids.reserve(feature_count);
      event.feature_summaries.reserve(feature_count);
      std::set<std::uint64_t> unique_feature_ids;
      for (std::size_t index = 8U; index < frame.sections.size(); ++index) {
        CP2TraceJournalFeatureSummary feature;
        if (!parse_feature_section(frame.sections[index], feature) ||
            !unique_feature_ids.insert(feature.feature_id).second) {
          result.status = CP2TraceJournalDecodeStatus::kInvalidSectionValue;
          return result;
        }
        feature_ids.push_back(feature.feature_id);
        event.feature_summaries.push_back(std::move(feature));
      }
      if (!ordered_subsequence(baseline_ids, feature_ids) ||
          !ordered_subsequence(candidate_ids, feature_ids)) {
        result.status = CP2TraceJournalDecodeStatus::kInvalidSectionValue;
        return result;
      }
      event.baseline_accepted_ids = std::move(baseline_ids);
      event.candidate_accepted_ids = std::move(candidate_ids);

      std::vector<std::uint8_t> event_state = state_header;
      std::vector<std::uint8_t> event_raw = raw_header;
      std::vector<std::uint8_t> event_proposal = proposal_header;
      if (!append_limited(event_state, frame.sections[5].bytes,
                          limits.maximum_total_bytes) ||
          !append_limited(event_raw, frame.sections[6].bytes,
                          limits.maximum_total_bytes) ||
          !append_limited(event_proposal, frame.sections[7].bytes,
                          limits.maximum_total_bytes)) {
        result.status = CP2TraceJournalDecodeStatus::kArithmeticOverflow;
        return result;
      }

      const std::vector<CP2StateTraceFrame> state_frames =
          CP2StateTraceCodec::DecodeStateFile(event_state, codec_limits);
      const std::vector<CP2RawSystemTraceFrame> raw_frames =
          CP2TraceCodec::DecodeRawSystemFile(event_raw, codec_limits);
      const std::vector<CP2ProposalTraceFrame> proposal_frames =
          CP2TraceCodec::DecodeProposalFile(event_proposal, codec_limits);
      std::uint64_t state_frame_count = 0U;
      std::uint64_t raw_frame_count = 0U;
      std::uint64_t proposal_frame_count = 0U;
      if (!size_to_u64(state_frames.size(), state_frame_count) ||
          !size_to_u64(raw_frames.size(), raw_frame_count) ||
          !size_to_u64(proposal_frames.size(), proposal_frame_count)) {
        result.status = CP2TraceJournalDecodeStatus::kArithmeticOverflow;
        return result;
      }
      const std::uint64_t expected_proposal_count =
          (baseline_proposal ? UINT64_C(1) : UINT64_C(0)) +
          (candidate_proposal ? UINT64_C(1) : UINT64_C(0));
      if (state_frame_count != event.state_phase_count ||
          raw_frame_count != event.raw_system_count ||
          proposal_frame_count != expected_proposal_count ||
          !exact_codec_file(event_state, state_frames) ||
          !exact_codec_file(event_raw, raw_frames) ||
          !exact_codec_file(event_proposal, proposal_frames)) {
        result.status =
            CP2TraceJournalDecodeStatus::kCanonicalCodecFailure;
        return result;
      }
      for (std::size_t index = 0U; index < state_frames.size(); ++index) {
        if (!same_identity(state_frames[index].invocation, event)) {
          result.status = CP2TraceJournalDecodeStatus::kIdentityViolation;
          return result;
        }
      }
      for (std::size_t index = 0U; index < raw_frames.size(); ++index) {
        if (!same_identity(raw_frames[index].invocation, event) ||
            index >= feature_ids.size() ||
            raw_frames[index].raw_system.feature_id != feature_ids[index]) {
          result.status = CP2TraceJournalDecodeStatus::kIdentityViolation;
          return result;
        }
      }
      std::size_t proposal_index = 0U;
      if (baseline_proposal) {
        if (proposal_index >= proposal_frames.size() ||
            proposal_frames[proposal_index].role !=
                CP2ProposalTraceRole::kNullspaceBaseline ||
            !same_identity(proposal_frames[proposal_index].invocation,
                           event)) {
          result.status = CP2TraceJournalDecodeStatus::kIdentityViolation;
          return result;
        }
        ++proposal_index;
      }
      if (candidate_proposal) {
        if (proposal_index >= proposal_frames.size() ||
            proposal_frames[proposal_index].role !=
                CP2ProposalTraceRole::kSchurCandidate ||
            !same_identity(proposal_frames[proposal_index].invocation,
                           event)) {
          result.status = CP2TraceJournalDecodeStatus::kIdentityViolation;
          return result;
        }
      }

      if (!append_limited(decoded.state_file_bytes,
                          frame.sections[5].bytes,
                          limits.maximum_total_bytes) ||
          !append_limited(decoded.raw_system_file_bytes,
                          frame.sections[6].bytes,
                          limits.maximum_total_bytes) ||
          !append_limited(decoded.proposal_file_bytes,
                          frame.sections[7].bytes,
                          limits.maximum_total_bytes)) {
        result.status = CP2TraceJournalDecodeStatus::kArithmeticOverflow;
        return result;
      }

      encoded_frame.clear();
      if (!append_frame_bytes(encoded_frame, frame)) {
        result.status = CP2TraceJournalDecodeStatus::kArithmeticOverflow;
        return result;
      }
      if (encoded_frame.size() != position - frame_begin ||
          !std::equal(encoded_frame.begin(), encoded_frame.end(),
                      bytes.begin() +
                          static_cast<std::ptrdiff_t>(frame_begin))) {
        result.status =
            CP2TraceJournalDecodeStatus::kCanonicalCodecFailure;
        return result;
      }
      event.sections.reserve(frame.sections.size());
      for (WireSection &section : frame.sections) {
        CP2TraceJournalDecodedSection retained;
        retained.tag =
            static_cast<CP2TraceJournalSectionTag>(section.tag);
        retained.bytes = std::move(section.bytes);
        event.sections.push_back(std::move(retained));
      }
      decoded.events.push_back(std::move(event));
      if (!checked_add(frame_count, UINT64_C(1), frame_count)) {
        result.status = CP2TraceJournalDecodeStatus::kArithmeticOverflow;
        return result;
      }
    }

    const std::vector<CP2StateTraceFrame> all_state_frames =
        CP2StateTraceCodec::DecodeStateFile(decoded.state_file_bytes,
                                            codec_limits);
    const std::vector<CP2RawSystemTraceFrame> all_raw_frames =
        CP2TraceCodec::DecodeRawSystemFile(decoded.raw_system_file_bytes,
                                           codec_limits);
    const std::vector<CP2ProposalTraceFrame> all_proposal_frames =
        CP2TraceCodec::DecodeProposalFile(decoded.proposal_file_bytes,
                                          codec_limits);
    if (!exact_codec_file(decoded.state_file_bytes, all_state_frames) ||
        !exact_codec_file(decoded.raw_system_file_bytes, all_raw_frames) ||
        !exact_codec_file(decoded.proposal_file_bytes,
                          all_proposal_frames)) {
      result.status = CP2TraceJournalDecodeStatus::kCanonicalCodecFailure;
      return result;
    }

    result.status = CP2TraceJournalDecodeStatus::kAccepted;
    result.journal = std::move(decoded);
    return result;
  } catch (const std::bad_alloc &) {
    result.status = CP2TraceJournalDecodeStatus::kAllocationFailure;
  } catch (const CP2TraceCodecError &) {
    result.status = CP2TraceJournalDecodeStatus::kCanonicalCodecFailure;
  } catch (...) {
    result.status = CP2TraceJournalDecodeStatus::kCanonicalCodecFailure;
  }
  result.journal = CP2TraceJournalDecoded{};
  return result;
}

const std::array<std::uint8_t, 32> &
ov_msckf::CP2TraceJournalSink::FileHeader() noexcept {
  static const std::array<std::uint8_t, 32> header = {{
      'S', 'c', 'h', 'u', 'r', 'V', 'I', 'O', '-', 'C', 'P', '2', '-', 'e',
      'v', 'e', 'n', 't', '-', 'j', 'o', 'u', 'r', 'n', 'a', 'l', '-', 'v',
      '1', '\n', 0U, 0U}};
  return header;
}

ov_msckf::CP2TraceJournalSink::CP2TraceJournalSink(
    std::shared_ptr<CP2TraceJournalWriter> writer,
    std::uint64_t maximum_journal_bytes) noexcept
    : writer_(std::move(writer)),
      maximum_journal_bytes_(maximum_journal_bytes) {
  if (!writer_) {
    Reject(CP2TraceJournalFailure::kWriterUnavailable);
    return;
  }
  try {
    const std::vector<CP2StateTraceFrame> no_state_frames;
    const std::vector<CP2ProposalTraceFrame> no_proposal_frames;
    const std::vector<CP2RawSystemTraceFrame> no_raw_frames;
    const std::vector<std::uint8_t> state_header =
        CP2StateTraceCodec::EncodeStateFile(no_state_frames).bytes;
    const std::vector<std::uint8_t> proposal_header =
        CP2TraceCodec::EncodeProposalFile(no_proposal_frames).bytes;
    const std::vector<std::uint8_t> raw_header =
        CP2TraceCodec::EncodeRawSystemFile(no_raw_frames).bytes;
    if (state_header.size() != kCanonicalFileHeaderSize ||
        proposal_header.size() != kCanonicalFileHeaderSize ||
        raw_header.size() != kCanonicalFileHeaderSize) {
      Reject(CP2TraceJournalFailure::kEncodingFailure);
      return;
    }

    std::vector<std::uint8_t> bootstrap_body;
    Encoder body_encoder(bootstrap_body);
    if (!body_encoder.U64(kFormatVersion) || !body_encoder.U64(UINT64_C(3)) ||
        !body_encoder.Section(kSectionStateFileHeader, state_header) ||
        !body_encoder.Section(kSectionProposalFileHeader, proposal_header) ||
        !body_encoder.Section(kSectionRawFileHeader, raw_header)) {
      Reject(CP2TraceJournalFailure::kArithmeticOverflow);
      return;
    }
    std::vector<std::uint8_t> bootstrap;
    Encoder bootstrap_encoder(bootstrap);
    std::uint64_t body_size = 0U;
    if (!size_to_u64(bootstrap_body.size(), body_size) ||
        !bootstrap_encoder.U64(body_size) ||
        !bootstrap_encoder.Bytes(bootstrap_body)) {
      Reject(CP2TraceJournalFailure::kArithmeticOverflow);
      return;
    }
    std::vector<std::uint8_t> preamble;
    Encoder preamble_encoder(preamble);
    const std::array<std::uint8_t, 32> &header = FileHeader();
    if (!preamble_encoder.Bytes(header.data(), header.size()) ||
        !preamble_encoder.Bytes(bootstrap)) {
      Reject(CP2TraceJournalFailure::kArithmeticOverflow);
      return;
    }
    if (!WriteAll(preamble.data(), preamble.size())) {
      return;
    }
  } catch (...) {
    Reject(CP2TraceJournalFailure::kEncodingFailure);
  }
}

bool ov_msckf::CP2TraceJournalSink::CreateForPreopenedFile(
    int descriptor, std::shared_ptr<CP2TraceJournalSink> &output,
    std::uint64_t maximum_journal_bytes) noexcept {
  output.reset();
  struct stat status {};
  const int flags = descriptor >= 0 ? ::fcntl(descriptor, F_GETFL) : -1;
  const off_t current = descriptor >= 0 ? ::lseek(descriptor, 0, SEEK_CUR) : -1;
  if (descriptor < 0 || flags < 0 || current != 0 ||
      (flags & O_ACCMODE) == O_RDONLY || ::fstat(descriptor, &status) != 0 ||
      !S_ISREG(status.st_mode) || status.st_nlink != 1 || status.st_size != 0) {
    return false;
  }
  const int duplicate = ::fcntl(descriptor, F_DUPFD_CLOEXEC, 3);
  if (duplicate < 0) {
    return false;
  }
  int unowned_duplicate = duplicate;
  try {
    std::unique_ptr<CP2TraceJournalWriter> owned_writer(
        new FileWriter(duplicate));
    unowned_duplicate = -1;
    std::shared_ptr<CP2TraceJournalWriter> writer(std::move(owned_writer));
    std::shared_ptr<CP2TraceJournalSink> candidate(
        new CP2TraceJournalSink(std::move(writer), maximum_journal_bytes));
    if (!candidate->ready()) {
      return false;
    }
    output = std::move(candidate);
    return true;
  } catch (...) {
    if (unowned_duplicate >= 0) {
      ::close(unowned_duplicate);
    }
    return false;
  }
}

void ov_msckf::CP2TraceJournalSink::Reject(
    CP2TraceJournalFailure failure) noexcept {
  if (failure_ == CP2TraceJournalFailure::kNone) {
    failure_ = failure;
  }
}

bool ov_msckf::CP2TraceJournalSink::WriteAll(const std::uint8_t *data,
                                             std::size_t size) noexcept {
  if (!ready() || !writer_ || (size != 0U && data == nullptr)) {
    Reject(CP2TraceJournalFailure::kWriteFailure);
    return false;
  }
  std::uint64_t wire_size = 0U;
  std::uint64_t final_size = 0U;
  if (!size_to_u64(size, wire_size) ||
      !checked_add(bytes_written_, wire_size, final_size) ||
      final_size > maximum_journal_bytes_) {
    Reject(CP2TraceJournalFailure::kArithmeticOverflow);
    return false;
  }
  std::size_t offset = 0U;
  while (offset < size) {
    const CP2TraceJournalWriteResult result =
        writer_->Write(data + offset, size - offset);
    if (result.bytes_written > size - offset) {
      Reject(CP2TraceJournalFailure::kWriteFailure);
      return false;
    }
    std::uint64_t committed = 0U;
    std::uint64_t next = 0U;
    if (!size_to_u64(result.bytes_written, committed) ||
        !checked_add(bytes_written_, committed, next)) {
      Reject(CP2TraceJournalFailure::kArithmeticOverflow);
      return false;
    }
    bytes_written_ = next;
    offset += result.bytes_written;
    if (!result.success || result.bytes_written == 0U) {
      Reject(CP2TraceJournalFailure::kWriteFailure);
      return false;
    }
  }
  return true;
}

bool ov_msckf::CP2TraceJournalSink::CheckIdentity(
    const CP2LiveUpdateEvent &update) noexcept {
  if (!update.invocation_context_available) {
    return false;
  }
  if (!have_identity_) {
    return update.invocation_id == 0U;
  }
  std::uint64_t expected_invocation = 0U;
  if (!checked_add(invocation_id_, UINT64_C(1), expected_invocation)) {
    Reject(CP2TraceJournalFailure::kArithmeticOverflow);
    return false;
  }
  if (update.sequence_index != sequence_index_ ||
      update.invocation_id != expected_invocation ||
      update.pair_index < pair_index_) {
    return false;
  }
  if (update.pair_index == pair_index_ &&
      update.camera_timestamp_ns != camera_timestamp_ns_) {
    return false;
  }
  return true;
}

bool ov_msckf::CP2TraceJournalSink::ValidateAndEncode(
    const CP2RecordedUpdateEvent &record,
    std::vector<std::uint8_t> &frame) {
  const CP2LiveUpdateEvent &update = record.update;
  if (!valid_terminal(update) || !update.timing_endpoint_valid ||
      update.timing_end_ns < update.timing_start_ns ||
      update.duration_ns !=
          update.timing_end_ns - update.timing_start_ns) {
    Reject(CP2TraceJournalFailure::kRecordInvariant);
    return false;
  }
  std::uint64_t raw_payload_count = 0U;
  std::uint64_t raw_input_count = 0U;
  std::uint64_t feature_count = 0U;
  if (!size_to_u64(record.raw_system_payloads.size(), raw_payload_count) ||
      !size_to_u64(update.shadow.input.raw_systems.size(), raw_input_count) ||
      !size_to_u64(update.shadow.result.features.size(), feature_count)) {
    Reject(CP2TraceJournalFailure::kArithmeticOverflow);
    return false;
  }
  const bool committed =
      update.terminal_status == CP2UpdateTerminalStatus::kCommittedCounted;
  const bool baseline_proposal_expected =
      update.shadow.baseline_preview_available &&
      update.shadow.live_nullspace_proposal.accepted();
  const bool candidate_proposal_expected =
      update.shadow.result.schur.proposal_available &&
      update.shadow.result.schur.proposal.accepted();
  const bool serialized_enums_valid =
      valid_enum(update.baseline_gamma_status, CP2GammaStatus::kNonfinite) &&
      valid_enum(update.shadow.result.nullspace.gamma_status,
                 CP2GammaStatus::kNonfinite) &&
      valid_enum(update.shadow.result.schur.gamma_status,
                 CP2GammaStatus::kNonfinite) &&
      valid_preview_enums(update.shadow.result.nullspace.proposal) &&
      valid_preview_enums(update.shadow.result.schur.proposal) &&
      valid_enum(record.baseline_commit_oracle.status,
                 CP2CommitOracleStatus::kComplete);
  const bool zero_raw_terminal_valid =
      update.terminal_status == CP2UpdateTerminalStatus::kEmptyInput ||
      (update.terminal_status == CP2UpdateTerminalStatus::kAllRejected &&
       update.terminal_subreason !=
           CP2UpdateTerminalSubreason::kAllBaselineFeaturesRejected) ||
      (update.terminal_status == CP2UpdateTerminalStatus::kInternalFailure &&
       update.terminal_subreason ==
           CP2UpdateTerminalSubreason::kTraceInvariantFailure);
  const bool nonzero_raw_terminal_valid =
      (update.terminal_status == CP2UpdateTerminalStatus::kAllRejected &&
       update.terminal_subreason ==
           CP2UpdateTerminalSubreason::kAllBaselineFeaturesRejected) ||
      update.terminal_status ==
          CP2UpdateTerminalStatus::kEmptyAfterCompression ||
      update.terminal_status == CP2UpdateTerminalStatus::kPreflightRejected ||
      update.terminal_status == CP2UpdateTerminalStatus::kCommittedCounted ||
      update.terminal_status == CP2UpdateTerminalStatus::kInternalFailure;
  const std::size_t required_phases =
      update.raw_system_count == 0U ? 0U : (committed ? 4U : 2U);
  if (record.state_phase_count != required_phases ||
      record.state_phase_count > record.state_phases.size() ||
      !serialized_enums_valid ||
      (update.raw_system_count == 0U ? !zero_raw_terminal_valid
                                    : !nonzero_raw_terminal_valid) ||
      update.raw_system_count > update.input_feature_count ||
      (update.terminal_status == CP2UpdateTerminalStatus::kEmptyInput &&
       update.input_feature_count != 0U) ||
      raw_payload_count != update.raw_system_count ||
      (update.raw_system_count != 0U &&
       (raw_input_count != update.raw_system_count ||
        feature_count != update.raw_system_count ||
        !update.shadow_evidence_available ||
        !update.shadow.shadow_math_completed ||
        !update.shadow.result.traversal_complete)) ||
      (update.raw_system_count == 0U &&
       (raw_input_count != 0U || feature_count != 0U ||
        update.shadow_evidence_available ||
        update.shadow.shadow_math_completed ||
        update.shadow.baseline_preview_available ||
        update.shadow.baseline_commit_planned ||
        update.shadow.result.traversal_complete ||
        update.shadow.result.raw_layouts_valid ||
        update.shadow.result.nullspace_assembly_valid ||
        update.shadow.result.schur_assembly_valid ||
        update.shadow.result.input_valid ||
        update.shadow.result.duplicate_feature_id)) ||
      record.baseline_proposal_payload_available !=
          !record.baseline_proposal_payload.empty() ||
      record.candidate_proposal_payload_available !=
          !record.candidate_proposal_payload.empty() ||
      record.baseline_proposal_payload_available !=
          baseline_proposal_expected ||
      record.candidate_proposal_payload_available !=
          candidate_proposal_expected ||
      update.shadow.result.nullspace.proposal_available !=
          update.shadow.result.nullspace.proposal.accepted() ||
      update.shadow.result.schur.proposal_available !=
          update.shadow.result.schur.proposal.accepted() ||
      committed != update.baseline_commit_occurred ||
      committed != record.baseline_commit_oracle_available ||
      (committed && !record.baseline_proposal_payload_available) ||
      (committed &&
       (record.baseline_commit_count != 1U ||
        record.baseline_mean_commit_count != 1U ||
        record.baseline_covariance_commit_count != 1U)) ||
      (!committed &&
       (record.baseline_commit_count != 0U ||
        record.baseline_mean_commit_count != 0U ||
        record.baseline_covariance_commit_count != 0U))) {
    Reject(CP2TraceJournalFailure::kRecordInvariant);
    return false;
  }
  for (std::size_t index = record.state_phase_count;
       index < record.state_phases.size(); ++index) {
    if (record.state_phases[index].snapshot ||
        !record.state_phases[index].payload.empty()) {
      Reject(CP2TraceJournalFailure::kRecordInvariant);
      return false;
    }
  }
  if (update.raw_system_count != 0U &&
      (update.baseline_accepted_ids !=
           update.shadow.result.nullspace.accepted_ids)) {
    Reject(CP2TraceJournalFailure::kRecordInvariant);
    return false;
  }

  CP2InvocationTraceIdentity identity;
  identity.sequence_index = update.sequence_index;
  identity.pair_index = update.pair_index;
  identity.invocation_id = update.invocation_id;

  std::vector<CP2StateTraceFrame> state_frames;
  state_frames.reserve(record.state_phase_count);
  for (std::size_t index = 0U; index < record.state_phase_count; ++index) {
    const CP2EncodedStatePhase &phase = record.state_phases[index];
    if (!phase.snapshot || enum_wire(phase.phase) != index ||
        phase.snapshot->phase != phase.phase) {
      Reject(CP2TraceJournalFailure::kRecordInvariant);
      return false;
    }
    CP2StateTraceFrame frame_value;
    frame_value.invocation = identity;
    frame_value.phase = phase.phase;
    frame_value.snapshot = *phase.snapshot;
    state_frames.push_back(std::move(frame_value));
  }
  const CP2EncodedStateFile encoded_state =
      CP2StateTraceCodec::EncodeStateFile(state_frames);
  if (encoded_state.payloads.size() != record.state_phase_count) {
    Reject(CP2TraceJournalFailure::kEncodingFailure);
    return false;
  }
  for (std::size_t index = 0U; index < record.state_phase_count; ++index) {
    if (!exact_payload(encoded_state.bytes, encoded_state.payloads[index],
                       record.state_phases[index].payload)) {
      Reject(CP2TraceJournalFailure::kRecordInvariant);
      return false;
    }
  }

  std::vector<CP2RawSystemTraceFrame> raw_frames;
  raw_frames.reserve(record.raw_system_payloads.size());
  std::set<std::uint64_t> raw_feature_ids;
  for (std::size_t index = 0U; index < record.raw_system_payloads.size();
       ++index) {
    std::uint64_t ordinal = 0U;
    if (!size_to_u64(index, ordinal) ||
        update.shadow.input.raw_systems[index].feature_id !=
            update.shadow.result.features[index].feature_id ||
        !raw_feature_ids
             .insert(update.shadow.input.raw_systems[index].feature_id)
             .second) {
      Reject(CP2TraceJournalFailure::kRecordInvariant);
      return false;
    }
    CP2RawSystemTraceFrame raw;
    raw.invocation = identity;
    raw.feature_ordinal = ordinal;
    raw.raw_system = update.shadow.input.raw_systems[index];
    raw_frames.push_back(std::move(raw));
  }
  std::vector<std::uint64_t> raw_feature_order;
  raw_feature_order.reserve(raw_frames.size());
  for (const CP2RawSystemTraceFrame &raw : raw_frames) {
    raw_feature_order.push_back(raw.raw_system.feature_id);
  }
  if (!ordered_subsequence(update.baseline_accepted_ids,
                           raw_feature_order) ||
      !ordered_subsequence(update.shadow.result.schur.accepted_ids,
                           raw_feature_order)) {
    Reject(CP2TraceJournalFailure::kRecordInvariant);
    return false;
  }
  const CP2EncodedRawSystemFile encoded_raw =
      CP2TraceCodec::EncodeRawSystemFile(raw_frames);
  if (encoded_raw.payloads.size() != record.raw_system_payloads.size()) {
    Reject(CP2TraceJournalFailure::kEncodingFailure);
    return false;
  }
  for (std::size_t index = 0U; index < record.raw_system_payloads.size();
       ++index) {
    if (!exact_payload(encoded_raw.bytes, encoded_raw.payloads[index],
                       record.raw_system_payloads[index])) {
      Reject(CP2TraceJournalFailure::kRecordInvariant);
      return false;
    }
  }

  std::vector<CP2ProposalTraceFrame> proposal_frames;
  if (record.baseline_proposal_payload_available) {
    if (!update.shadow.baseline_preview_available ||
        !update.shadow.live_nullspace_proposal.accepted()) {
      Reject(CP2TraceJournalFailure::kRecordInvariant);
      return false;
    }
    CP2ProposalTraceFrame proposal;
    proposal.invocation = identity;
    proposal.role = CP2ProposalTraceRole::kNullspaceBaseline;
    proposal.proposal.dx = update.shadow.live_nullspace_proposal.dx;
    proposal.proposal.P_plus = update.shadow.live_nullspace_proposal.P_plus;
    proposal_frames.push_back(std::move(proposal));
  }
  if (record.candidate_proposal_payload_available) {
    if (!update.shadow.result.schur.proposal_available ||
        !update.shadow.result.schur.proposal.accepted()) {
      Reject(CP2TraceJournalFailure::kRecordInvariant);
      return false;
    }
    CP2ProposalTraceFrame proposal;
    proposal.invocation = identity;
    proposal.role = CP2ProposalTraceRole::kSchurCandidate;
    proposal.proposal.dx = update.shadow.result.schur.proposal.dx;
    proposal.proposal.P_plus = update.shadow.result.schur.proposal.P_plus;
    proposal_frames.push_back(std::move(proposal));
  }
  const CP2EncodedProposalFile encoded_proposal =
      CP2TraceCodec::EncodeProposalFile(proposal_frames);
  if (encoded_proposal.payloads.size() != proposal_frames.size()) {
    Reject(CP2TraceJournalFailure::kEncodingFailure);
    return false;
  }
  std::size_t proposal_index = 0U;
  if (record.baseline_proposal_payload_available &&
      !exact_payload(encoded_proposal.bytes,
                     encoded_proposal.payloads[proposal_index++],
                     record.baseline_proposal_payload)) {
    Reject(CP2TraceJournalFailure::kRecordInvariant);
    return false;
  }
  if (record.candidate_proposal_payload_available &&
      !exact_payload(encoded_proposal.bytes,
                     encoded_proposal.payloads[proposal_index],
                     record.candidate_proposal_payload)) {
    Reject(CP2TraceJournalFailure::kRecordInvariant);
    return false;
  }

  std::vector<std::uint8_t> state_fragment;
  std::vector<std::uint8_t> raw_fragment;
  std::vector<std::uint8_t> proposal_fragment;
  if (!strip_header(encoded_state.bytes, state_fragment) ||
      !strip_header(encoded_raw.bytes, raw_fragment) ||
      !strip_header(encoded_proposal.bytes, proposal_fragment)) {
    Reject(CP2TraceJournalFailure::kEncodingFailure);
    return false;
  }

  std::vector<std::vector<std::uint8_t>> feature_sections;
  feature_sections.reserve(update.shadow.result.features.size());
  for (const CP2FeaturePairResult &feature :
       update.shadow.result.features) {
    if (!valid_feature_enums(feature)) {
      Reject(CP2TraceJournalFailure::kRecordInvariant);
      return false;
    }
    feature_sections.emplace_back();
    if (!append_feature(feature, feature_sections.back())) {
      Reject(CP2TraceJournalFailure::kArithmeticOverflow);
      return false;
    }
  }

  std::vector<std::uint8_t> core;
  Encoder core_encoder(core);
  if (!core_encoder.U64(update.sequence_index) ||
      !core_encoder.U64(update.pair_index) ||
      !core_encoder.U64(update.camera_timestamp_ns) ||
      !core_encoder.U64(update.invocation_id) ||
      !core_encoder.U64(update.duration_ns) ||
      !core_encoder.U64(enum_wire(update.terminal_status)) ||
      !core_encoder.U64(enum_wire(update.terminal_subreason)) ||
      !core_encoder.U64(update.input_feature_count) ||
      !core_encoder.U64(update.raw_system_count) ||
      !core_encoder.U64(event_flags(record)) ||
      !core_encoder.U64(enum_wire(update.baseline_gamma_status)) ||
      !core_encoder.Binary64(update.baseline_gamma) ||
      !core_encoder.U64(update.baseline_precompression_rows) ||
      !core_encoder.U64(update.baseline_compressed_rows) ||
      !core_encoder.U64(record.baseline_commit_count) ||
      !core_encoder.U64(record.baseline_mean_commit_count) ||
      !core_encoder.U64(record.baseline_covariance_commit_count) ||
      !core_encoder.U64(record.candidate_ekf_update_call_count) ||
      !core_encoder.U64(record.candidate_mean_write_count) ||
      !core_encoder.U64(record.candidate_covariance_write_count) ||
      !core_encoder.U64(record.candidate_type_update_call_count) ||
      !core_encoder.U64(record.candidate_feature_write_count) ||
      !core_encoder.U64(raw_payload_count) ||
      !core_encoder.U64(record.state_phase_count) ||
      !core_encoder.U64(feature_count)) {
    Reject(CP2TraceJournalFailure::kArithmeticOverflow);
    return false;
  }

  std::vector<std::uint8_t> baseline_ids;
  std::vector<std::uint8_t> candidate_ids;
  if (!append_ids(update.baseline_accepted_ids, baseline_ids) ||
      !append_ids(update.shadow.result.schur.accepted_ids, candidate_ids)) {
    Reject(CP2TraceJournalFailure::kRecordInvariant);
    return false;
  }

  std::vector<std::uint8_t> global;
  Encoder global_encoder(global);
  std::uint64_t nullspace_precompression = 0U;
  std::uint64_t nullspace_compressed = 0U;
  std::uint64_t candidate_precompression = 0U;
  std::uint64_t candidate_compressed = 0U;
  if (!count_index_to_wire(
          update.shadow.result.nullspace.precompression_rows,
          nullspace_precompression) ||
      !count_index_to_wire(update.shadow.result.nullspace.compressed_rows,
                           nullspace_compressed) ||
      !count_index_to_wire(update.shadow.result.schur.precompression_rows,
                           candidate_precompression) ||
      !count_index_to_wire(update.shadow.result.schur.compressed_rows,
                           candidate_compressed) ||
      !global_encoder.U64(
          enum_wire(update.shadow.result.nullspace.gamma_status)) ||
      !global_encoder.Binary64(
          update.shadow.result.nullspace.retained_gamma) ||
      !global_encoder.U64(enum_wire(update.shadow.result.schur.gamma_status)) ||
      !global_encoder.Binary64(update.shadow.result.schur.retained_gamma) ||
      !global_encoder.U64(nullspace_precompression) ||
      !global_encoder.U64(nullspace_compressed) ||
      !global_encoder.U64(candidate_precompression) ||
      !global_encoder.U64(candidate_compressed) ||
      !append_preview(update.shadow.result.nullspace.proposal,
                      global_encoder) ||
      !append_preview(update.shadow.result.schur.proposal, global_encoder)) {
    Reject(CP2TraceJournalFailure::kArithmeticOverflow);
    return false;
  }

  std::vector<std::uint8_t> oracle;
  Encoder oracle_encoder(oracle);
  const CP2CommitOracleResult &oracle_value = record.baseline_commit_oracle;
  std::uint64_t oracle_flags = 0U;
  const auto oracle_bit = [&oracle_flags](unsigned index, bool value) {
    if (value) {
      oracle_flags |= UINT64_C(1) << index;
    }
  };
  oracle_bit(0U, oracle_value.comparison_available);
  oracle_bit(1U, oracle_value.structure_equal);
  oracle_bit(2U, oracle_value.phase0_valid);
  oracle_bit(3U, oracle_value.phase2_valid);
  oracle_bit(4U, oracle_value.phase3_valid);
  oracle_bit(5U, oracle_value.complete_finite);
  oracle_bit(6U, oracle_value.phase0_phase2_immutable_equal);
  oracle_bit(7U, oracle_value.phase2_phase3_canonical_equal);
  oracle_bit(8U, oracle_value.complete_canonical_equal);
  oracle_bit(9U, oracle_value.type_update_calls_match);
  oracle_bit(10U, oracle_value.passed);
  if (!oracle_encoder.U64(enum_wire(oracle_value.status)) ||
      !oracle_encoder.U64(oracle_flags) ||
      !oracle_encoder.U64(oracle_value.observed_type_update_calls) ||
      !oracle_encoder.U64(
          oracle_value.baseline_expected_type_update_calls) ||
      !oracle_encoder.U64(
          oracle_value.baseline_verified_nominal_fields) ||
      !oracle_encoder.U64(oracle_value.baseline_nominal_mismatches) ||
      !oracle_encoder.U64(oracle_value.baseline_covariance_mismatches) ||
      !oracle_encoder.U64(oracle_value.baseline_fej_mismatches) ||
      !oracle_encoder.U64(oracle_value.state_blocks_expected) ||
      !oracle_encoder.U64(oracle_value.state_blocks_seen) ||
      !oracle_encoder.U64(oracle_value.covariance_blocks_expected) ||
      !oracle_encoder.U64(oracle_value.covariance_blocks_seen)) {
    Reject(CP2TraceJournalFailure::kArithmeticOverflow);
    return false;
  }

  std::uint64_t section_count = UINT64_C(8);
  if (!checked_add(section_count, feature_count, section_count)) {
    Reject(CP2TraceJournalFailure::kArithmeticOverflow);
    return false;
  }
  std::vector<std::uint8_t> body;
  Encoder body_encoder(body);
  if (!body_encoder.U64(kFormatVersion) ||
      !body_encoder.U64(section_count) ||
      !body_encoder.Section(kSectionEventCore, core) ||
      !body_encoder.Section(kSectionBaselineAcceptedIds, baseline_ids) ||
      !body_encoder.Section(kSectionCandidateAcceptedIds, candidate_ids) ||
      !body_encoder.Section(kSectionGlobalShadow, global) ||
      !body_encoder.Section(kSectionCommitOracle, oracle) ||
      !body_encoder.Section(kSectionStateFileFragment, state_fragment) ||
      !body_encoder.Section(kSectionRawFileFragment, raw_fragment) ||
      !body_encoder.Section(kSectionProposalFileFragment,
                            proposal_fragment)) {
    Reject(CP2TraceJournalFailure::kArithmeticOverflow);
    return false;
  }
  for (const std::vector<std::uint8_t> &feature : feature_sections) {
    if (!body_encoder.Section(kSectionFeatureSummary, feature)) {
      Reject(CP2TraceJournalFailure::kArithmeticOverflow);
      return false;
    }
  }

  Encoder frame_encoder(frame);
  std::uint64_t body_size = 0U;
  if (!size_to_u64(body.size(), body_size) || !frame_encoder.U64(body_size) ||
      !frame_encoder.Bytes(body)) {
    Reject(CP2TraceJournalFailure::kArithmeticOverflow);
    return false;
  }
  return true;
}

ov_msckf::CP2RecordedSinkStatus ov_msckf::CP2TraceJournalSink::Publish(
    const std::shared_ptr<const CP2RecordedUpdateEvent> &record) noexcept {
  if (!ready() || finalized_) {
    return CP2RecordedSinkStatus::kRejected;
  }
  if (!record) {
    Reject(CP2TraceJournalFailure::kNullRecord);
    return CP2RecordedSinkStatus::kRejected;
  }
  if (!CheckIdentity(record->update)) {
    if (ready()) {
      Reject(CP2TraceJournalFailure::kIdentityViolation);
    }
    return CP2RecordedSinkStatus::kRejected;
  }
  try {
    std::vector<std::uint8_t> frame;
    if (!ValidateAndEncode(*record, frame)) {
      return CP2RecordedSinkStatus::kRejected;
    }
    std::uint64_t next_records = 0U;
    if (!checked_add(records_written_, UINT64_C(1), next_records)) {
      Reject(CP2TraceJournalFailure::kArithmeticOverflow);
      return CP2RecordedSinkStatus::kRejected;
    }
    if (!WriteAll(frame.data(), frame.size())) {
      return CP2RecordedSinkStatus::kRejected;
    }
    records_written_ = next_records;
    have_identity_ = true;
    sequence_index_ = record->update.sequence_index;
    pair_index_ = record->update.pair_index;
    camera_timestamp_ns_ = record->update.camera_timestamp_ns;
    invocation_id_ = record->update.invocation_id;
    return CP2RecordedSinkStatus::kPublished;
  } catch (const std::bad_alloc &) {
    Reject(CP2TraceJournalFailure::kEncodingFailure);
  } catch (...) {
    Reject(CP2TraceJournalFailure::kEncodingFailure);
  }
  return CP2RecordedSinkStatus::kRejected;
}

bool ov_msckf::CP2TraceJournalSink::Finalize() noexcept {
  if (finalized_) {
    return ready();
  }
  finalized_ = true;
  if (!ready() || !writer_ || !writer_->Sync(bytes_written_)) {
    Reject(CP2TraceJournalFailure::kSyncFailure);
    return false;
  }
  return true;
}
