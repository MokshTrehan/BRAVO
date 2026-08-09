/*
 * SchurVIO-Lite CP2 authoritative updater event journal.
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#ifndef OV_MSCKF_CP2_TRACE_JOURNAL_H
#define OV_MSCKF_CP2_TRACE_JOURNAL_H

#include "UpdaterMSCKF.h"

#include <array>
#include <cstddef>
#include <cstdint>
#include <memory>
#include <vector>

namespace ov_msckf {

/** One nonthrowing write attempt made by an injectable journal writer. */
struct CP2TraceJournalWriteResult {
  bool success = false;
  std::size_t bytes_written = 0U;
};

/**
 * Narrow synchronous writer used by the authoritative sink.
 *
 * A successful short write is legal and is completed by the sink. A failed
 * write may report a committed prefix; that prefix is included in the sink's
 * byte count before the journal becomes permanently rejected.
 */
class CP2TraceJournalWriter {
public:
  virtual ~CP2TraceJournalWriter() = default;
  virtual CP2TraceJournalWriteResult
  Write(const std::uint8_t *data, std::size_t size) noexcept = 0;

  /** Seal durable output at exactly expected_size bytes. */
  virtual bool Sync(std::uint64_t expected_size) noexcept {
    (void)expected_size;
    return false;
  }
};

/** Sticky first-failure classification for a CP2TraceJournalSink. */
enum class CP2TraceJournalFailure : std::uint8_t {
  kNone,
  kWriterUnavailable,
  kFileInvalid,
  kNullRecord,
  kIdentityViolation,
  kRecordInvariant,
  kArithmeticOverflow,
  kEncodingFailure,
  kWriteFailure,
  kSyncFailure,
};

const char *
cp2_trace_journal_failure_name(CP2TraceJournalFailure failure) noexcept;

/** Public wire tags retained by the strict lossless decoder. */
enum class CP2TraceJournalSectionTag : std::uint64_t {
  kEventCore = UINT64_C(1),
  kBaselineAcceptedIds = UINT64_C(2),
  kCandidateAcceptedIds = UINT64_C(3),
  kGlobalShadow = UINT64_C(4),
  kCommitOracle = UINT64_C(5),
  kStateFileFragment = UINT64_C(6),
  kRawFileFragment = UINT64_C(7),
  kProposalFileFragment = UINT64_C(8),
  kFeatureSummary = UINT64_C(10),
  kStateFileHeader = UINT64_C(101),
  kProposalFileHeader = UINT64_C(102),
  kRawFileHeader = UINT64_C(103),
};

/** Explicit input-size and structural limits for untrusted journal bytes. */
struct CP2TraceJournalDecodeLimits {
  std::uint64_t maximum_total_bytes = UINT64_C(2147483648);
  std::uint64_t maximum_frames = UINT64_C(1000000);
  std::uint64_t maximum_sections_per_frame = UINT64_C(1000010);
  std::uint64_t maximum_section_bytes = UINT64_C(536870912);
};

/** One exact owning event section in its canonical on-wire order. */
struct CP2TraceJournalDecodedSection {
  CP2TraceJournalSectionTag tag =
      CP2TraceJournalSectionTag::kEventCore;
  std::vector<std::uint8_t> bytes;
};

/** Exact u64 counter set shared by journal preview, reducer, and gate summaries. */
struct CP2TraceJournalCounterSummary {
  std::uint64_t jitter = 0U;
  std::uint64_t repair = 0U;
  std::uint64_t alternate_solve = 0U;
  std::uint64_t clamp = 0U;
  std::uint64_t regularization = 0U;
  std::uint64_t silent_fallback = 0U;
  std::uint64_t fallback = 0U;
};

/** Lossless typed view of the fixed event-core section (tag 1). */
struct CP2TraceJournalCoreSummary {
  std::uint64_t duration_ns = 0U;
  CP2UpdateTerminalStatus terminal_status =
      CP2UpdateTerminalStatus::kInternalFailure;
  CP2UpdateTerminalSubreason terminal_subreason =
      CP2UpdateTerminalSubreason::kTraceInvariantFailure;
  std::uint64_t input_feature_count = 0U;
  std::uint64_t raw_system_count = 0U;

  bool baseline_precompression_system_nonempty = false;
  bool baseline_precompression_rows_available = false;
  bool baseline_compressed_system_nonempty = false;
  bool baseline_compressed_rows_available = false;
  bool baseline_preflight_attempted = false;
  bool baseline_preflight_accepted = false;
  bool baseline_commit_occurred = false;
  bool shadow_evidence_available = false;
  bool phase01_canonical_equal = false;
  bool phase01_pointer_graph_equal = false;
  bool baseline_proposal_payload_available = false;
  bool candidate_proposal_payload_available = false;
  bool baseline_commit_oracle_available = false;
  bool baseline_commit_mismatch = false;
  bool online_math_evidence_passed = false;
  bool shadow_math_completed = false;
  bool baseline_preview_available = false;
  bool baseline_commit_planned = false;
  bool traversal_complete = false;
  bool raw_layouts_valid = false;
  bool nullspace_assembly_valid = false;
  bool schur_assembly_valid = false;
  bool input_valid = false;
  bool duplicate_feature_id = false;
  bool nullspace_proposal_available = false;
  bool schur_proposal_available = false;

  CP2GammaStatus baseline_gamma_status = CP2GammaStatus::kNotReached;
  double baseline_gamma = 0.0;
  std::uint64_t baseline_precompression_rows = 0U;
  std::uint64_t baseline_compressed_rows = 0U;
  std::uint64_t baseline_commit_count = 0U;
  std::uint64_t baseline_mean_commit_count = 0U;
  std::uint64_t baseline_covariance_commit_count = 0U;
  std::uint64_t candidate_ekf_update_call_count = 0U;
  std::uint64_t candidate_mean_write_count = 0U;
  std::uint64_t candidate_covariance_write_count = 0U;
  std::uint64_t candidate_type_update_call_count = 0U;
  std::uint64_t candidate_feature_write_count = 0U;
  std::uint64_t raw_payload_count = 0U;
  std::uint64_t state_phase_count = 0U;
  std::uint64_t feature_summary_count = 0U;
};

/** Lossless typed preview diagnostics carried by tag 4. */
struct CP2TraceJournalPreviewSummary {
  MSCKFUpdatePreviewStatus status = MSCKFUpdatePreviewStatus::kInvalidInput;
  MSCKFUpdatePreviewStage stage = MSCKFUpdatePreviewStage::kState;
  std::uint64_t state_dimension = 0U;
  std::uint64_t measurement_dimension = 0U;
  std::uint64_t jacobian_dimension = 0U;
  std::uint64_t ordered_jacobian_dimension = 0U;
  std::int64_t offending_order_index = -1;
  std::int64_t offending_diagonal_index = -1;
  bool minimum_posterior_diagonal_available = false;
  double minimum_posterior_diagonal = 0.0;
  CP2TraceJournalCounterSummary counters;
};

/** Lossless typed view of the global shadow section (tag 4). */
struct CP2TraceJournalGlobalSummary {
  CP2GammaStatus nullspace_gamma_status = CP2GammaStatus::kNotReached;
  double nullspace_retained_gamma = 0.0;
  CP2GammaStatus schur_gamma_status = CP2GammaStatus::kNotReached;
  double schur_retained_gamma = 0.0;
  std::uint64_t nullspace_precompression_rows = 0U;
  std::uint64_t nullspace_compressed_rows = 0U;
  std::uint64_t schur_precompression_rows = 0U;
  std::uint64_t schur_compressed_rows = 0U;
  CP2TraceJournalPreviewSummary nullspace_preview;
  CP2TraceJournalPreviewSummary schur_preview;
};

/** Lossless typed gate evidence carried inside a tag-10 feature summary. */
struct CP2TraceJournalGateSummary {
  CP2FeatureGateStage stage = CP2FeatureGateStage::kReductionUnavailable;
  std::uint64_t degrees_of_freedom = 0U;
  bool chi2_available = false;
  double chi2 = 0.0;
  bool threshold_available = false;
  double threshold = 0.0;
  bool evidence_decision_available = false;
  bool evidence_accept = false;
  bool lifecycle_accept = false;
  CP2TraceJournalCounterSummary counters;
};

/** Lossless typed sufficient-statistic comparison from a feature summary. */
struct CP2TraceJournalStatisticSummary {
  CP2StatisticComparisonStatus status =
      CP2StatisticComparisonStatus::kNotRequired;
  bool available = false;
  double reference_norm = 0.0;
  double error = 0.0;
  double tolerance = 0.0;
  double ratio = 0.0;
  bool passed = false;
};

/** Lossless typed view of one fixed feature-summary section (tag 10). */
struct CP2TraceJournalFeatureSummary {
  std::uint64_t feature_id = 0U;
  std::uint64_t raw_rows = 0U;
  bool raw_layout_valid = false;

  CP2NullspaceReductionStatus nullspace_status =
      CP2NullspaceReductionStatus::kInvalidDimensions;
  CP2NullspaceReductionStage nullspace_stage =
      CP2NullspaceReductionStage::kInputDimensions;
  std::uint64_t nullspace_raw_rows = 0U;
  std::uint64_t nullspace_reduced_rows = 0U;
  bool nullspace_emitted_system_available = false;
  bool nullspace_mode_gamma_available = false;
  double nullspace_mode_gamma = 0.0;
  bool nullspace_raw_lambda_symmetry_error_available = false;
  bool nullspace_lambda_available = false;
  bool nullspace_eta_available = false;
  bool nullspace_gamma_available = false;
  double nullspace_raw_lambda_symmetry_error_inf = 0.0;
  double nullspace_gamma = 0.0;
  CP2TraceJournalCounterSummary nullspace_reducer_counters;

  SchurReductionStatus schur_status = SchurReductionStatus::kNonfinite;
  SchurReductionStage schur_stage = SchurReductionStage::kInputDimensions;
  std::uint64_t schur_raw_rows = 0U;
  std::uint64_t schur_degrees_of_freedom = 0U;
  bool schur_singular_values_available = false;
  bool schur_ratio_available = false;
  std::array<double, 3> schur_singular_values{{0.0, 0.0, 0.0}};
  double schur_ratio = 0.0;
  double schur_raw_lambda_symmetry_error_inf = 0.0;
  double schur_gamma = 0.0;
  CP2TraceJournalCounterSummary schur_reducer_counters;

  CP2TraceJournalGateSummary nullspace_gate;
  CP2TraceJournalGateSummary schur_gate;
  bool statistics_comparison_required = false;
  CP2TraceJournalStatisticSummary lambda_comparison;
  CP2TraceJournalStatisticSummary eta_comparison;
  CP2TraceJournalStatisticSummary gamma_comparison;
  CP2GateAgreementClass agreement_class =
      CP2GateAgreementClass::kNeitherDecision;
  std::uint64_t raw_row_match_weight = 0U;
};

/**
 * One strict decoded event plus the identity/count fields parsed from tag 1.
 *
 * sections retains every byte needed to reproduce the event frame. The cached
 * fields are validated against the core and the reconstructed codec frames.
 */
struct CP2TraceJournalDecodedEvent {
  std::uint64_t sequence_index = 0U;
  std::uint64_t pair_index = 0U;
  std::uint64_t camera_timestamp_ns = 0U;
  std::uint64_t invocation_id = 0U;
  std::uint64_t raw_system_count = 0U;
  std::uint64_t state_phase_count = 0U;
  std::uint64_t feature_summary_count = 0U;
  CP2TraceJournalCoreSummary core;
  std::vector<std::uint64_t> baseline_accepted_ids;
  std::vector<std::uint64_t> candidate_accepted_ids;
  CP2TraceJournalGlobalSummary global;
  CP2CommitOracleResult commit_oracle;
  std::vector<CP2TraceJournalFeatureSummary> feature_summaries;
  std::vector<CP2TraceJournalDecodedSection> sections;
};

/** Complete owning decode and the three directly replayable codec files. */
struct CP2TraceJournalDecoded {
  std::vector<CP2TraceJournalDecodedEvent> events;
  std::vector<std::uint8_t> state_file_bytes;
  std::vector<std::uint8_t> proposal_file_bytes;
  std::vector<std::uint8_t> raw_system_file_bytes;
};

enum class CP2TraceJournalDecodeStatus : std::uint8_t {
  kAccepted,
  kTotalBytesLimit,
  kInvalidHeader,
  kTrailingBytes,
  kTruncatedFrame,
  kArithmeticOverflow,
  kFrameLimit,
  kSectionLimit,
  kSectionBytesLimit,
  kInvalidVersion,
  kInvalidBootstrap,
  kInvalidSectionInventory,
  kInvalidSectionValue,
  kIdentityViolation,
  kCanonicalCodecFailure,
  kAllocationFailure,
};

const char *cp2_trace_journal_decode_status_name(
    CP2TraceJournalDecodeStatus status) noexcept;

struct CP2TraceJournalDecodeResult {
  CP2TraceJournalDecodeStatus status =
      CP2TraceJournalDecodeStatus::kInvalidHeader;
  CP2TraceJournalDecoded journal;

  bool accepted() const noexcept {
    return status == CP2TraceJournalDecodeStatus::kAccepted;
  }
};

/** Strict, failure-atomic decoder and canonical codec-stream assembler. */
class CP2TraceJournalCodec {
public:
  static CP2TraceJournalDecodeResult Decode(
      const std::vector<std::uint8_t> &bytes,
      const CP2TraceJournalDecodeLimits &limits =
          CP2TraceJournalDecodeLimits()) noexcept;
};

/**
 * Deterministic, synchronous CP2RecordedUpdateSink implementation.
 *
 * The journal starts with the exact 32 bytes
 * `SchurVIO-CP2-event-journal-v1\n\0\0`, followed by one bootstrap frame and
 * then one frame for each event:
 *
 *   u64-be frame-body length
 *   u64-be format version (=1)
 *   u64-be section count
 *   repeated {u64-be section tag, u64-be section length, section bytes}
 *
 * The bootstrap carries the exact canonical 32-byte state, proposal, and raw
 * file headers as tags 101, 102, and 103. Event sections are emitted in this
 * exact order: event core (tag 1), baseline
 * accepted IDs (2), candidate accepted IDs (3), global shadow summary (4),
 * commit oracle (5), canonical state-file fragment (6), canonical raw-file
 * fragment (7), canonical proposal-file fragment (8), and feature summaries
 * in raw traversal order (10). Concatenating each bootstrap codec header with
 * all corresponding event fragments reconstructs that codec's deterministic
 * append-only file. Every scalar in a journal-owned section is u64-be;
 * Boolean values are exactly zero or one and binary64 values are their raw
 * IEEE-754 bits. Section lengths, rather than native object layout, delimit
 * variable payload bytes. The format therefore contains no pointer, padding,
 * host-endian integer, or estimator object.
 *
 * Publish validates the complete record before emitting its frame, retains no
 * record or estimator reference, and performs no estimator write. Any invalid
 * record, checked-arithmetic failure, or writer failure is sticky: that call
 * and every later call returns CP2RecordedSinkStatus::kRejected.
 */
class CP2TraceJournalSink final : public CP2RecordedUpdateSink {
public:
  static constexpr std::uint64_t kFormatVersion = UINT64_C(1);
  static constexpr std::uint64_t kDefaultMaximumJournalBytes =
      UINT64_MAX;

  explicit CP2TraceJournalSink(
      std::shared_ptr<CP2TraceJournalWriter> writer,
      std::uint64_t maximum_journal_bytes =
          kDefaultMaximumJournalBytes) noexcept;

  ~CP2TraceJournalSink() override = default;

  CP2TraceJournalSink(const CP2TraceJournalSink &) = delete;
  CP2TraceJournalSink &operator=(const CP2TraceJournalSink &) = delete;

  /**
   * Duplicate and own a preopened empty writable regular single-link file.
   *
   * The input descriptor remains caller-owned. The factory never truncates or
   * replaces a path. It fails unless
   * fstat proves a regular single-link file, the open description is writable
   * and seekable, and both size and current offset are zero. The owned
   * close-on-exec duplicate shares that open-description offset, which advances
   * with journal writes.
   * After a successful factory call, the caller must cease all I/O and seeking
   * through the original descriptor until the sink has been finalized or
   * destroyed; every write and final sync verifies exclusive sequential use.
   *
   * A descriptor cannot reveal whether its opener followed a symbolic link.
   * The caller must therefore have opened the selected path with O_NOFOLLOW
   * (or an equivalent trusted no-follow operation) before calling this API.
   */
  static bool CreateForPreopenedFile(
      int descriptor, std::shared_ptr<CP2TraceJournalSink> &output,
      std::uint64_t maximum_journal_bytes =
          kDefaultMaximumJournalBytes) noexcept;

  CP2RecordedSinkStatus
  Publish(const std::shared_ptr<const CP2RecordedUpdateEvent> &record)
      noexcept override;

  /**
   * Seal the append stream and establish its explicit durability boundary.
   *
   * The first call permanently prevents later Publish calls. It succeeds only
   * when the sink has no sticky failure and the writer verifies and syncs
   * exactly bytes_written() bytes. Repeated calls are idempotent.
   */
  bool Finalize() noexcept;

  bool ready() const noexcept {
    return failure_ == CP2TraceJournalFailure::kNone;
  }
  CP2TraceJournalFailure failure() const noexcept { return failure_; }
  bool finalized() const noexcept { return finalized_; }
  std::uint64_t records_written() const noexcept { return records_written_; }
  std::uint64_t bytes_written() const noexcept { return bytes_written_; }

  static const std::array<std::uint8_t, 32> &FileHeader() noexcept;

private:
  bool ValidateAndEncode(const CP2RecordedUpdateEvent &record,
                         std::vector<std::uint8_t> &frame);
  bool WriteAll(const std::uint8_t *data, std::size_t size) noexcept;
  bool CheckIdentity(const CP2LiveUpdateEvent &update) noexcept;
  void Reject(CP2TraceJournalFailure failure) noexcept;

  std::shared_ptr<CP2TraceJournalWriter> writer_;
  std::uint64_t maximum_journal_bytes_ = 0U;
  std::uint64_t records_written_ = 0U;
  std::uint64_t bytes_written_ = 0U;
  bool have_identity_ = false;
  std::uint64_t sequence_index_ = 0U;
  std::uint64_t pair_index_ = 0U;
  std::uint64_t camera_timestamp_ns_ = 0U;
  std::uint64_t invocation_id_ = 0U;
  bool finalized_ = false;
  CP2TraceJournalFailure failure_ = CP2TraceJournalFailure::kNone;
};

} // namespace ov_msckf

#endif // OV_MSCKF_CP2_TRACE_JOURNAL_H
