/*
 * SchurVIO-Lite CP2 authoritative journal tests.
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include "update/CP2TraceJournal.h"

#include "update/CP2StateTraceCodec.h"
#include "update/CP2TraceCodec.h"

#include <gtest/gtest.h>

#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <fcntl.h>
#include <limits>
#include <memory>
#include <string>
#include <sys/stat.h>
#include <unistd.h>
#include <utility>
#include <vector>

namespace {

using ov_msckf::CP2RecordedSinkStatus;
using ov_msckf::CP2RecordedUpdateEvent;
using ov_msckf::CP2TraceJournalDecodeStatus;
using ov_msckf::CP2TraceJournalFailure;
using ov_msckf::CP2TraceJournalSink;

class MemoryWriter final : public ov_msckf::CP2TraceJournalWriter {
public:
  ov_msckf::CP2TraceJournalWriteResult
  Write(const std::uint8_t *data, std::size_t size) noexcept override {
    ++write_calls;
    if (size != 0U && data == nullptr) {
      return {false, 0U};
    }
    if (fail_enabled && bytes.size() >= fail_after_bytes) {
      return {false, 0U};
    }
    std::size_t amount = std::min(size, maximum_chunk);
    if (fail_enabled) {
      const std::size_t remaining = fail_after_bytes - bytes.size();
      amount = std::min(amount, remaining);
    }
    bytes.insert(bytes.end(), data, data + amount);
    const bool failed_now = fail_enabled && bytes.size() >= fail_after_bytes;
    return {!failed_now, amount};
  }

  bool Sync(std::uint64_t expected_size) noexcept override {
    ++sync_calls;
    sync_expected_size = expected_size;
    return sync_succeeds && expected_size == bytes.size();
  }

  std::vector<std::uint8_t> bytes;
  std::size_t maximum_chunk = std::numeric_limits<std::size_t>::max();
  bool fail_enabled = false;
  std::size_t fail_after_bytes = std::numeric_limits<std::size_t>::max();
  bool sync_succeeds = true;
  std::size_t write_calls = 0U;
  std::size_t sync_calls = 0U;
  std::uint64_t sync_expected_size = 0U;
};

class WriteOnlyWriter final : public ov_msckf::CP2TraceJournalWriter {
public:
  ov_msckf::CP2TraceJournalWriteResult
  Write(const std::uint8_t *data, std::size_t size) noexcept override {
    bytes.insert(bytes.end(), data, data + size);
    return {true, size};
  }
  std::vector<std::uint8_t> bytes;
};

void append_u64(std::vector<std::uint8_t> &bytes, std::uint64_t value) {
  for (unsigned index = 0U; index < 8U; ++index) {
    bytes.push_back(
        static_cast<std::uint8_t>(value >> (56U - 8U * index)));
  }
}

std::uint64_t load_u64(const std::vector<std::uint8_t> &bytes,
                       std::size_t offset) {
  if (offset > bytes.size() || bytes.size() - offset < 8U) {
    throw std::runtime_error("test u64 offset is outside bytes");
  }
  std::uint64_t value = 0U;
  for (std::size_t index = 0U; index < 8U; ++index) {
    value = (value << 8U) | bytes[offset + index];
  }
  return value;
}

void store_u64(std::vector<std::uint8_t> &bytes, std::size_t offset,
               std::uint64_t value) {
  if (offset > bytes.size() || bytes.size() - offset < 8U) {
    throw std::runtime_error("test u64 offset is outside bytes");
  }
  for (std::size_t index = 0U; index < 8U; ++index) {
    bytes[offset + index] =
        static_cast<std::uint8_t>(value >> (56U - 8U * index));
  }
}

struct SectionLocation {
  std::size_t tag = 0U;
  std::size_t length = 0U;
  std::size_t data = 0U;
  std::size_t size = 0U;
};

struct FrameLocation {
  std::size_t beginning = 0U;
  std::size_t body_length = 0U;
  std::size_t version = 0U;
  std::size_t section_count = 0U;
  std::size_t end = 0U;
  std::vector<SectionLocation> sections;
};

FrameLocation locate_frame(const std::vector<std::uint8_t> &bytes,
                           std::size_t beginning) {
  FrameLocation frame;
  frame.beginning = beginning;
  frame.body_length = beginning;
  const std::uint64_t body_size = load_u64(bytes, beginning);
  frame.version = beginning + 8U;
  frame.section_count = beginning + 16U;
  frame.end = beginning + 8U + static_cast<std::size_t>(body_size);
  std::size_t cursor = beginning + 24U;
  const std::uint64_t count = load_u64(bytes, frame.section_count);
  for (std::uint64_t index = 0U; index < count; ++index) {
    SectionLocation section;
    section.tag = cursor;
    section.length = cursor + 8U;
    section.data = cursor + 16U;
    section.size = static_cast<std::size_t>(load_u64(bytes, section.length));
    frame.sections.push_back(section);
    cursor = section.data + section.size;
  }
  if (cursor != frame.end || frame.end > bytes.size()) {
    throw std::runtime_error("test frame locator found malformed bytes");
  }
  return frame;
}

std::vector<std::uint8_t> known_preamble() {
  static const char state_header[] =
      "SchurVIO-CP2-state-file-v1\n\0\0\0\0\0";
  static const char proposal_header[] =
      "SchurVIO-CP2-proposal-file-v1\n\0\0";
  static const char raw_header[] =
      "SchurVIO-CP2-raw-file-v1\n\0\0\0\0\0\0\0";
  static_assert(sizeof(state_header) - 1U == 32U, "state header size");
  static_assert(sizeof(proposal_header) - 1U == 32U,
                "proposal header size");
  static_assert(sizeof(raw_header) - 1U == 32U, "raw header size");

  std::vector<std::uint8_t> body;
  append_u64(body, 1U);
  append_u64(body, 3U);
  const auto append_section = [&body](std::uint64_t tag, const char *data) {
    append_u64(body, tag);
    append_u64(body, 32U);
    body.insert(body.end(), data, data + 32U);
  };
  append_section(101U, state_header);
  append_section(102U, proposal_header);
  append_section(103U, raw_header);

  const std::array<std::uint8_t, 32> &header =
      CP2TraceJournalSink::FileHeader();
  std::vector<std::uint8_t> result(header.begin(), header.end());
  append_u64(result, body.size());
  result.insert(result.end(), body.begin(), body.end());
  return result;
}

std::shared_ptr<CP2RecordedUpdateEvent> make_zero_event(
    std::uint64_t invocation_id, std::uint64_t pair_index,
    std::uint64_t timestamp_ns,
    ov_msckf::CP2UpdateTerminalStatus status =
        ov_msckf::CP2UpdateTerminalStatus::kEmptyInput,
    ov_msckf::CP2UpdateTerminalSubreason subreason =
        ov_msckf::CP2UpdateTerminalSubreason::kInputEmpty,
    std::uint64_t input_count = 0U) {
  auto event = std::make_shared<CP2RecordedUpdateEvent>();
  event->update.invocation_context_available = true;
  event->update.sequence_index = 7U;
  event->update.pair_index = pair_index;
  event->update.camera_timestamp_ns = timestamp_ns;
  event->update.invocation_id = invocation_id;
  event->update.timing_endpoint_valid = true;
  event->update.timing_start_ns = 10000U + invocation_id;
  event->update.duration_ns = 1234U + invocation_id;
  event->update.timing_end_ns =
      event->update.timing_start_ns + event->update.duration_ns;
  event->update.terminal_status = status;
  event->update.terminal_subreason = subreason;
  event->update.input_feature_count = input_count;
  event->online_math_evidence_passed =
      status != ov_msckf::CP2UpdateTerminalStatus::kInternalFailure;
  return event;
}

void install_phase(CP2RecordedUpdateEvent &event, std::size_t index,
                   ov_msckf::CP2StatePhase phase, double timestamp) {
  ov_msckf::CP2CompositeStateSnapshot snapshot;
  snapshot.phase = phase;
  snapshot.timestamp = timestamp;
  snapshot.covariance.resize(0, 0);
  snapshot.time_offset_nominal.resize(0, 0);
  snapshot.time_offset_fej.resize(0, 0);
  event.state_phases[index].phase = phase;
  event.state_phases[index].payload =
      ov_msckf::CP2StateTraceCodec::EncodeSnapshotPayload(snapshot);
  event.state_phases[index].snapshot.reset(
      new ov_msckf::CP2CompositeStateSnapshot(std::move(snapshot)));
}

ov_msckf::CP2RawFeatureSystem raw_fixture(std::uint64_t feature_id) {
  ov_msckf::CP2RawFeatureSystem raw;
  raw.feature_id = feature_id;
  raw.H_x.resize(1, 1);
  raw.H_x << -0.0;
  raw.H_f.resize(1, 3);
  raw.H_f << 1.25, -2.5, 3.75;
  raw.residual.resize(1);
  raw.residual << -4.5;
  raw.jacobian_layout.emplace_back(0, 1, 0);
  return raw;
}

std::shared_ptr<CP2RecordedUpdateEvent> make_raw_rejected_event(
    std::uint64_t invocation_id = 0U, std::uint64_t pair_index = 4U,
    std::uint64_t timestamp_ns = 500U) {
  auto event = make_zero_event(
      invocation_id, pair_index, timestamp_ns,
      ov_msckf::CP2UpdateTerminalStatus::kAllRejected,
      ov_msckf::CP2UpdateTerminalSubreason::kAllBaselineFeaturesRejected, 1U);
  const ov_msckf::CP2RawFeatureSystem raw = raw_fixture(55U);
  event->update.raw_system_count = 1U;
  event->update.baseline_gamma_status = ov_msckf::CP2GammaStatus::kAvailable;
  event->update.baseline_gamma = 2.25;
  event->update.shadow_evidence_available = true;
  event->update.shadow.shadow_math_completed = true;
  event->update.shadow.input.raw_systems.push_back(raw);
  event->update.shadow.result.traversal_complete = true;
  event->update.shadow.result.raw_layouts_valid = true;
  event->update.shadow.result.nullspace_assembly_valid = true;
  event->update.shadow.result.schur_assembly_valid = true;
  event->update.shadow.result.input_valid = true;
  event->update.shadow.result.nullspace.gamma_status =
      ov_msckf::CP2GammaStatus::kAvailable;
  event->update.shadow.result.nullspace.retained_gamma = 2.25;
  event->update.shadow.result.schur.gamma_status =
      ov_msckf::CP2GammaStatus::kAvailable;
  event->update.shadow.result.schur.retained_gamma = 2.25;
  ov_msckf::CP2FeaturePairResult feature;
  feature.feature_id = raw.feature_id;
  feature.raw_rows = 1;
  feature.raw_layout_valid = true;
  feature.nullspace.raw_rows = 1;
  feature.schur.raw_rows = 1;
  event->update.shadow.result.features.push_back(feature);
  event->raw_system_payloads.push_back(
      ov_msckf::CP2TraceCodec::EncodeRawSystemPayload(raw));
  install_phase(*event, 0U, ov_msckf::CP2StatePhase::kPhase0Prior, 1.5);
  install_phase(*event, 1U,
                ov_msckf::CP2StatePhase::kPhase1Precommit, 1.5);
  event->state_phase_count = 2U;
  event->phase01_canonical_equal = true;
  event->phase01_pointer_graph_equal = true;
  event->online_math_evidence_passed = true;
  return event;
}

ov_msckf::MSCKFUpdatePreviewResult proposal_fixture(double dx,
                                                     double variance) {
  ov_msckf::MSCKFUpdatePreviewResult proposal;
  proposal.diagnostics.status =
      ov_msckf::MSCKFUpdatePreviewStatus::kAccepted;
  proposal.diagnostics.stage =
      ov_msckf::MSCKFUpdatePreviewStage::kAccepted;
  proposal.diagnostics.state_dimension = 1;
  proposal.diagnostics.measurement_dimension = 1;
  proposal.diagnostics.jacobian_dimension = 1;
  proposal.diagnostics.ordered_jacobian_dimension = 1;
  proposal.dx.resize(1);
  proposal.dx << dx;
  proposal.P_plus.resize(1, 1);
  proposal.P_plus << variance;
  return proposal;
}

std::vector<std::uint8_t> proposal_payload(
    const ov_msckf::MSCKFUpdatePreviewResult &proposal) {
  ov_msckf::CP2ProposalTracePayload payload;
  payload.dx = proposal.dx;
  payload.P_plus = proposal.P_plus;
  return ov_msckf::CP2TraceCodec::EncodeProposalPayload(payload);
}

std::shared_ptr<CP2RecordedUpdateEvent> make_committed_event() {
  auto event = make_raw_rejected_event();
  event->update.terminal_status =
      ov_msckf::CP2UpdateTerminalStatus::kCommittedCounted;
  event->update.terminal_subreason =
      ov_msckf::CP2UpdateTerminalSubreason::kNone;
  event->update.baseline_accepted_ids = {55U};
  event->update.shadow.result.nullspace.accepted_ids = {55U};
  event->update.shadow.result.schur.accepted_ids = {55U};
  event->update.baseline_preflight_attempted = true;
  event->update.baseline_preflight_accepted = true;
  event->update.baseline_commit_occurred = true;
  event->update.shadow.baseline_preview_available = true;
  event->update.shadow.baseline_commit_planned = true;

  const ov_msckf::MSCKFUpdatePreviewResult baseline =
      proposal_fixture(-0.0, 0.75);
  const ov_msckf::MSCKFUpdatePreviewResult candidate =
      proposal_fixture(0.125, 0.875);
  event->update.shadow.live_nullspace_proposal = baseline;
  event->update.shadow.result.nullspace.proposal_available = true;
  event->update.shadow.result.nullspace.proposal = baseline;
  event->update.shadow.result.schur.proposal_available = true;
  event->update.shadow.result.schur.proposal = candidate;
  event->baseline_proposal_payload_available = true;
  event->baseline_proposal_payload = proposal_payload(baseline);
  event->candidate_proposal_payload_available = true;
  event->candidate_proposal_payload = proposal_payload(candidate);

  install_phase(*event, 2U,
                ov_msckf::CP2StatePhase::kPhase2ExpectedPostcommit, 1.5);
  install_phase(*event, 3U,
                ov_msckf::CP2StatePhase::kPhase3LivePostcommit, 1.5);
  event->state_phase_count = 4U;
  event->baseline_commit_oracle_available = true;
  event->baseline_commit_oracle.status =
      ov_msckf::CP2CommitOracleStatus::kComplete;
  event->baseline_commit_oracle.comparison_available = true;
  event->baseline_commit_oracle.structure_equal = true;
  event->baseline_commit_oracle.phase0_valid = true;
  event->baseline_commit_oracle.phase2_valid = true;
  event->baseline_commit_oracle.phase3_valid = true;
  event->baseline_commit_oracle.complete_finite = true;
  event->baseline_commit_oracle.phase0_phase2_immutable_equal = true;
  event->baseline_commit_oracle.phase2_phase3_canonical_equal = true;
  event->baseline_commit_oracle.complete_canonical_equal = true;
  event->baseline_commit_oracle.type_update_calls_match = true;
  event->baseline_commit_oracle.passed = true;
  event->baseline_commit_count = 1U;
  event->baseline_mean_commit_count = 1U;
  event->baseline_covariance_commit_count = 1U;
  event->online_math_evidence_passed = true;
  return event;
}

std::shared_ptr<CP2RecordedUpdateEvent> make_two_raw_committed_event() {
  auto event = make_committed_event();
  const ov_msckf::CP2RawFeatureSystem second = raw_fixture(77U);
  event->update.input_feature_count = 2U;
  event->update.raw_system_count = 2U;
  event->update.shadow.input.raw_systems.push_back(second);
  ov_msckf::CP2FeaturePairResult feature =
      event->update.shadow.result.features[0];
  feature.feature_id = second.feature_id;
  event->update.shadow.result.features.push_back(feature);
  event->raw_system_payloads.push_back(
      ov_msckf::CP2TraceCodec::EncodeRawSystemPayload(second));
  event->update.baseline_accepted_ids.push_back(second.feature_id);
  event->update.shadow.result.nullspace.accepted_ids.push_back(
      second.feature_id);
  event->update.shadow.result.schur.accepted_ids.push_back(second.feature_id);
  return event;
}

std::vector<std::uint8_t> selected_payload(
    const std::vector<std::uint8_t> &file,
    const ov_msckf::CP2TracePayloadReference &reference) {
  const std::size_t offset = static_cast<std::size_t>(reference.offset);
  const std::size_t length = static_cast<std::size_t>(reference.length);
  return std::vector<std::uint8_t>(file.begin() + offset,
                                   file.begin() + offset + length);
}

void expect_decode_status(const std::vector<std::uint8_t> &bytes,
                          CP2TraceJournalDecodeStatus expected) {
  const ov_msckf::CP2TraceJournalDecodeResult result =
      ov_msckf::CP2TraceJournalCodec::Decode(bytes);
  EXPECT_EQ(result.status, expected)
      << ov_msckf::cp2_trace_journal_decode_status_name(result.status);
  if (expected != CP2TraceJournalDecodeStatus::kAccepted) {
    EXPECT_TRUE(result.journal.events.empty());
    EXPECT_TRUE(result.journal.state_file_bytes.empty());
    EXPECT_TRUE(result.journal.proposal_file_bytes.empty());
    EXPECT_TRUE(result.journal.raw_system_file_bytes.empty());
  }
}

struct TemporaryFile {
  TemporaryFile() {
    std::array<char, 64> name{{}};
    const char pattern[] = "/tmp/cp2-trace-journal-XXXXXX";
    std::copy(pattern, pattern + sizeof(pattern), name.begin());
    descriptor = ::mkstemp(name.data());
    path = name.data();
  }
  ~TemporaryFile() {
    if (descriptor >= 0) {
      ::close(descriptor);
    }
    if (!path.empty()) {
      ::unlink(path.c_str());
    }
  }
  int descriptor = -1;
  std::string path;
};

TEST(CP2TraceJournalFormat, FrozenBootstrapKnownAnswerAndEmptyDecode) {
  auto writer = std::make_shared<MemoryWriter>();
  CP2TraceJournalSink sink(writer);
  ASSERT_TRUE(sink.ready());
  EXPECT_EQ(writer->bytes, known_preamble());
  EXPECT_EQ(sink.bytes_written(), writer->bytes.size());

  const auto decoded = ov_msckf::CP2TraceJournalCodec::Decode(writer->bytes);
  ASSERT_TRUE(decoded.accepted())
      << ov_msckf::cp2_trace_journal_decode_status_name(decoded.status);
  EXPECT_TRUE(decoded.journal.events.empty());
  EXPECT_EQ(decoded.journal.state_file_bytes,
            ov_msckf::CP2StateTraceCodec::EncodeStateFile({}).bytes);
  EXPECT_EQ(decoded.journal.proposal_file_bytes,
            ov_msckf::CP2TraceCodec::EncodeProposalFile({}).bytes);
  EXPECT_EQ(decoded.journal.raw_system_file_bytes,
            ov_msckf::CP2TraceCodec::EncodeRawSystemFile({}).bytes);
}

TEST(CP2TraceJournalWriter, ShortWritesRemainExactAndFinalizeSeals) {
  auto writer = std::make_shared<MemoryWriter>();
  writer->maximum_chunk = 3U;
  CP2TraceJournalSink sink(writer);
  ASSERT_TRUE(sink.ready());
  ASSERT_EQ(sink.Publish(make_zero_event(0U, 5U, 900U)),
            CP2RecordedSinkStatus::kPublished);
  EXPECT_GT(writer->write_calls, 10U);
  expect_decode_status(writer->bytes, CP2TraceJournalDecodeStatus::kAccepted);

  const std::vector<std::uint8_t> sealed_bytes = writer->bytes;
  EXPECT_TRUE(sink.Finalize());
  EXPECT_TRUE(sink.finalized());
  EXPECT_TRUE(sink.ready());
  EXPECT_EQ(writer->sync_calls, 1U);
  EXPECT_EQ(writer->sync_expected_size, sealed_bytes.size());
  EXPECT_EQ(sink.Publish(make_zero_event(1U, 6U, 901U)),
            CP2RecordedSinkStatus::kRejected);
  EXPECT_EQ(writer->bytes, sealed_bytes);
  EXPECT_TRUE(sink.Finalize());
  EXPECT_EQ(writer->sync_calls, 1U);
}

TEST(CP2TraceJournalWriter, PartialFailureIsCountedAndRejectionIsSticky) {
  auto writer = std::make_shared<MemoryWriter>();
  CP2TraceJournalSink sink(writer);
  ASSERT_TRUE(sink.ready());
  const std::size_t preamble_size = writer->bytes.size();
  writer->fail_enabled = true;
  writer->fail_after_bytes = preamble_size + 11U;
  EXPECT_EQ(sink.Publish(make_zero_event(0U, 1U, 10U)),
            CP2RecordedSinkStatus::kRejected);
  EXPECT_EQ(sink.failure(), CP2TraceJournalFailure::kWriteFailure);
  EXPECT_EQ(sink.bytes_written(), writer->bytes.size());
  EXPECT_EQ(writer->bytes.size(), preamble_size + 11U);
  EXPECT_EQ(sink.records_written(), 0U);

  const std::size_t calls_after_failure = writer->write_calls;
  const std::vector<std::uint8_t> bytes_after_failure = writer->bytes;
  EXPECT_EQ(sink.Publish(make_zero_event(0U, 1U, 10U)),
            CP2RecordedSinkStatus::kRejected);
  EXPECT_EQ(writer->write_calls, calls_after_failure);
  EXPECT_EQ(writer->bytes, bytes_after_failure);
  EXPECT_FALSE(sink.Finalize());
  EXPECT_TRUE(sink.finalized());
  EXPECT_EQ(writer->sync_calls, 0U);
}

TEST(CP2TraceJournalWriter, SyncFailureIsStickyAndSealsPublication) {
  auto writer = std::make_shared<MemoryWriter>();
  CP2TraceJournalSink sink(writer);
  ASSERT_EQ(sink.Publish(make_zero_event(0U, 1U, 10U)),
            CP2RecordedSinkStatus::kPublished);
  writer->sync_succeeds = false;
  const std::vector<std::uint8_t> before_sync = writer->bytes;
  EXPECT_FALSE(sink.Finalize());
  EXPECT_TRUE(sink.finalized());
  EXPECT_EQ(sink.failure(), CP2TraceJournalFailure::kSyncFailure);
  EXPECT_EQ(writer->sync_calls, 1U);
  EXPECT_EQ(sink.Publish(make_zero_event(1U, 2U, 20U)),
            CP2RecordedSinkStatus::kRejected);
  EXPECT_EQ(writer->bytes, before_sync);
  EXPECT_FALSE(sink.Finalize());
  EXPECT_EQ(writer->sync_calls, 1U);
}

TEST(CP2TraceJournalWriter, WriterWithoutExplicitSyncFailsClosed) {
  auto writer = std::make_shared<WriteOnlyWriter>();
  CP2TraceJournalSink sink(writer);
  ASSERT_EQ(sink.Publish(make_zero_event(0U, 1U, 10U)),
            CP2RecordedSinkStatus::kPublished);
  EXPECT_FALSE(sink.Finalize());
  EXPECT_EQ(sink.failure(), CP2TraceJournalFailure::kSyncFailure);
  EXPECT_TRUE(sink.finalized());
}

TEST(CP2TraceJournalWriter, ByteBudgetOverflowWritesNoPartialUnit) {
  auto probe = std::make_shared<MemoryWriter>();
  CP2TraceJournalSink probe_sink(probe);
  ASSERT_TRUE(probe_sink.ready());
  const std::uint64_t preamble_size = probe_sink.bytes_written();
  ASSERT_GT(preamble_size, 0U);

  auto too_small_writer = std::make_shared<MemoryWriter>();
  CP2TraceJournalSink too_small(too_small_writer, preamble_size - 1U);
  EXPECT_FALSE(too_small.ready());
  EXPECT_EQ(too_small.failure(), CP2TraceJournalFailure::kArithmeticOverflow);
  EXPECT_TRUE(too_small_writer->bytes.empty());

  auto exact_writer = std::make_shared<MemoryWriter>();
  CP2TraceJournalSink exact(exact_writer, preamble_size);
  ASSERT_TRUE(exact.ready());
  const std::vector<std::uint8_t> before = exact_writer->bytes;
  EXPECT_EQ(exact.Publish(make_zero_event(0U, 1U, 1U)),
            CP2RecordedSinkStatus::kRejected);
  EXPECT_EQ(exact.failure(), CP2TraceJournalFailure::kArithmeticOverflow);
  EXPECT_EQ(exact_writer->bytes, before);
  EXPECT_EQ(exact.records_written(), 0U);
}

TEST(CP2TraceJournalIdentity,
     ContiguousInvocationsAllowRepeatedPairAndRegressingNewTimestamp) {
  auto first_writer = std::make_shared<MemoryWriter>();
  CP2TraceJournalSink first(first_writer);
  ASSERT_EQ(first.Publish(make_zero_event(0U, 4U, 1000U)),
            CP2RecordedSinkStatus::kPublished);
  ASSERT_EQ(first.Publish(make_zero_event(1U, 4U, 1000U)),
            CP2RecordedSinkStatus::kPublished);
  ASSERT_EQ(first.Publish(make_zero_event(2U, 8U, 25U)),
            CP2RecordedSinkStatus::kPublished);
  EXPECT_EQ(first.records_written(), 3U);
  expect_decode_status(first_writer->bytes,
                       CP2TraceJournalDecodeStatus::kAccepted);

  auto second_writer = std::make_shared<MemoryWriter>();
  CP2TraceJournalSink second(second_writer);
  ASSERT_EQ(second.Publish(make_zero_event(0U, 4U, 1000U)),
            CP2RecordedSinkStatus::kPublished);
  ASSERT_EQ(second.Publish(make_zero_event(1U, 4U, 1000U)),
            CP2RecordedSinkStatus::kPublished);
  ASSERT_EQ(second.Publish(make_zero_event(2U, 8U, 25U)),
            CP2RecordedSinkStatus::kPublished);
  EXPECT_EQ(first_writer->bytes, second_writer->bytes);
}

TEST(CP2TraceJournalIdentity, DuplicateSkippedAndReorderedIdentityAreSticky) {
  struct InvalidIdentity {
    std::uint64_t invocation;
    std::uint64_t pair;
    std::uint64_t timestamp;
    bool change_sequence;
  };
  const std::array<InvalidIdentity, 5> invalid{{
      {0U, 2U, 20U, false},
      {2U, 2U, 20U, false},
      {1U, 0U, 20U, false},
      {1U, 1U, 21U, false},
      {1U, 2U, 20U, true},
  }};
  for (const InvalidIdentity &value : invalid) {
    auto writer = std::make_shared<MemoryWriter>();
    CP2TraceJournalSink sink(writer);
    ASSERT_EQ(sink.Publish(make_zero_event(0U, 1U, 20U)),
              CP2RecordedSinkStatus::kPublished);
    auto record = make_zero_event(value.invocation, value.pair,
                                  value.timestamp);
    if (value.change_sequence) {
      record->update.sequence_index += 1U;
    }
    const std::vector<std::uint8_t> before = writer->bytes;
    EXPECT_EQ(sink.Publish(record), CP2RecordedSinkStatus::kRejected);
    EXPECT_EQ(sink.failure(), CP2TraceJournalFailure::kIdentityViolation);
    EXPECT_EQ(writer->bytes, before);
    EXPECT_EQ(sink.Publish(make_zero_event(1U, 2U, 20U)),
              CP2RecordedSinkStatus::kRejected);
    EXPECT_EQ(writer->bytes, before);
  }

  auto writer = std::make_shared<MemoryWriter>();
  CP2TraceJournalSink sink(writer);
  EXPECT_EQ(sink.Publish(make_zero_event(1U, 1U, 1U)),
            CP2RecordedSinkStatus::kRejected);
  EXPECT_EQ(sink.failure(), CP2TraceJournalFailure::kIdentityViolation);
}

TEST(CP2TraceJournalEvents, EveryLegalZeroRawTerminalRoundTrips) {
  struct Terminal {
    ov_msckf::CP2UpdateTerminalStatus status;
    ov_msckf::CP2UpdateTerminalSubreason subreason;
    std::uint64_t input_count;
  };
  const std::array<Terminal, 5> terminals{{
      {ov_msckf::CP2UpdateTerminalStatus::kEmptyInput,
       ov_msckf::CP2UpdateTerminalSubreason::kInputEmpty, 0U},
      {ov_msckf::CP2UpdateTerminalStatus::kAllRejected,
       ov_msckf::CP2UpdateTerminalSubreason::kNoFeaturesAfterCleaning, 2U},
      {ov_msckf::CP2UpdateTerminalStatus::kAllRejected,
       ov_msckf::CP2UpdateTerminalSubreason::kNoFeaturesAfterTriangulation,
       2U},
      {ov_msckf::CP2UpdateTerminalStatus::kAllRejected,
       ov_msckf::CP2UpdateTerminalSubreason::kNoRawSystems, 2U},
      {ov_msckf::CP2UpdateTerminalStatus::kInternalFailure,
       ov_msckf::CP2UpdateTerminalSubreason::kTraceInvariantFailure, 2U},
  }};
  for (const Terminal &terminal : terminals) {
    auto writer = std::make_shared<MemoryWriter>();
    CP2TraceJournalSink sink(writer);
    ASSERT_EQ(sink.Publish(make_zero_event(0U, 1U, 2U, terminal.status,
                                           terminal.subreason,
                                           terminal.input_count)),
              CP2RecordedSinkStatus::kPublished);
    const auto decoded =
        ov_msckf::CP2TraceJournalCodec::Decode(writer->bytes);
    ASSERT_TRUE(decoded.accepted());
    ASSERT_EQ(decoded.journal.events.size(), 1U);
    EXPECT_EQ(decoded.journal.events[0].raw_system_count, 0U);
    EXPECT_EQ(decoded.journal.events[0].state_phase_count, 0U);
    EXPECT_TRUE(ov_msckf::CP2StateTraceCodec::DecodeStateFile(
                    decoded.journal.state_file_bytes)
                    .empty());
    EXPECT_TRUE(ov_msckf::CP2TraceCodec::DecodeRawSystemFile(
                    decoded.journal.raw_system_file_bytes)
                    .empty());
    EXPECT_TRUE(ov_msckf::CP2TraceCodec::DecodeProposalFile(
                    decoded.journal.proposal_file_bytes)
                    .empty());
  }
}

TEST(CP2TraceJournalEvents, ImpossibleTerminalAndHiddenPhaseSuffixReject) {
  auto writer = std::make_shared<MemoryWriter>();
  CP2TraceJournalSink sink(writer);
  auto impossible = make_zero_event(
      0U, 1U, 2U,
      ov_msckf::CP2UpdateTerminalStatus::kEmptyAfterCompression,
      ov_msckf::CP2UpdateTerminalSubreason::kMeasurementCompressionEmpty, 1U);
  EXPECT_EQ(sink.Publish(impossible), CP2RecordedSinkStatus::kRejected);
  EXPECT_EQ(sink.failure(), CP2TraceJournalFailure::kRecordInvariant);

  auto suffix_writer = std::make_shared<MemoryWriter>();
  CP2TraceJournalSink suffix_sink(suffix_writer);
  auto suffix = make_zero_event(0U, 1U, 2U);
  install_phase(*suffix, 1U,
                ov_msckf::CP2StatePhase::kPhase1Precommit, 1.0);
  EXPECT_EQ(suffix_sink.Publish(suffix), CP2RecordedSinkStatus::kRejected);
  EXPECT_EQ(suffix_sink.failure(), CP2TraceJournalFailure::kRecordInvariant);
}

TEST(CP2TraceJournalEvents,
     InvalidReversedAndMismatchedTimingEndpointsRejectBeforeWrite) {
  for (std::size_t mutation = 0U; mutation < 3U; ++mutation) {
    auto writer = std::make_shared<MemoryWriter>();
    CP2TraceJournalSink sink(writer);
    auto event = make_zero_event(0U, 1U, 2U);
    if (mutation == 0U) {
      event->update.timing_endpoint_valid = false;
    } else if (mutation == 1U) {
      event->update.timing_end_ns = event->update.timing_start_ns - 1U;
    } else {
      event->update.duration_ns += 1U;
    }
    const std::vector<std::uint8_t> before = writer->bytes;
    EXPECT_EQ(sink.Publish(event), CP2RecordedSinkStatus::kRejected);
    EXPECT_EQ(sink.failure(), CP2TraceJournalFailure::kRecordInvariant);
    EXPECT_EQ(writer->bytes, before);
    EXPECT_EQ(sink.records_written(), 0U);
  }
}

TEST(CP2TraceJournalEvents, DuplicateRawFeatureAndInvalidEnumRejectExplicitly) {
  auto duplicate_writer = std::make_shared<MemoryWriter>();
  CP2TraceJournalSink duplicate_sink(duplicate_writer);
  auto duplicate = make_raw_rejected_event();
  duplicate->update.input_feature_count = 2U;
  duplicate->update.raw_system_count = 2U;
  duplicate->update.shadow.input.raw_systems.push_back(
      duplicate->update.shadow.input.raw_systems[0]);
  duplicate->update.shadow.result.features.push_back(
      duplicate->update.shadow.result.features[0]);
  duplicate->raw_system_payloads.push_back(duplicate->raw_system_payloads[0]);
  const std::vector<std::uint8_t> duplicate_before = duplicate_writer->bytes;
  EXPECT_EQ(duplicate_sink.Publish(duplicate),
            CP2RecordedSinkStatus::kRejected);
  EXPECT_EQ(duplicate_sink.failure(),
            CP2TraceJournalFailure::kRecordInvariant);
  EXPECT_EQ(duplicate_writer->bytes, duplicate_before);

  auto enum_writer = std::make_shared<MemoryWriter>();
  CP2TraceJournalSink enum_sink(enum_writer);
  auto invalid_enum = make_raw_rejected_event();
  invalid_enum->update.shadow.result.features[0].agreement_class =
      static_cast<ov_msckf::CP2GateAgreementClass>(999);
  EXPECT_EQ(enum_sink.Publish(invalid_enum),
            CP2RecordedSinkStatus::kRejected);
  EXPECT_EQ(enum_sink.failure(), CP2TraceJournalFailure::kRecordInvariant);

  auto id_writer = std::make_shared<MemoryWriter>();
  CP2TraceJournalSink id_sink(id_writer);
  auto invalid_id = make_committed_event();
  invalid_id->update.baseline_accepted_ids[0] = 999U;
  invalid_id->update.shadow.result.nullspace.accepted_ids[0] = 999U;
  EXPECT_EQ(id_sink.Publish(invalid_id), CP2RecordedSinkStatus::kRejected);
  EXPECT_EQ(id_sink.failure(), CP2TraceJournalFailure::kRecordInvariant);

  auto order_writer = std::make_shared<MemoryWriter>();
  CP2TraceJournalSink order_sink(order_writer);
  auto invalid_order = make_two_raw_committed_event();
  std::reverse(invalid_order->update.baseline_accepted_ids.begin(),
               invalid_order->update.baseline_accepted_ids.end());
  invalid_order->update.shadow.result.nullspace.accepted_ids =
      invalid_order->update.baseline_accepted_ids;
  EXPECT_EQ(order_sink.Publish(invalid_order),
            CP2RecordedSinkStatus::kRejected);
  EXPECT_EQ(order_sink.failure(), CP2TraceJournalFailure::kRecordInvariant);
}

TEST(CP2TraceJournalPayloads,
     OwningStateRawAndProposalBytesReconstructExactly) {
  auto writer = std::make_shared<MemoryWriter>();
  CP2TraceJournalSink sink(writer);
  const auto event = make_committed_event();
  ASSERT_EQ(sink.Publish(event), CP2RecordedSinkStatus::kPublished);
  const auto decoded = ov_msckf::CP2TraceJournalCodec::Decode(writer->bytes);
  ASSERT_TRUE(decoded.accepted())
      << ov_msckf::cp2_trace_journal_decode_status_name(decoded.status);
  ASSERT_EQ(decoded.journal.events.size(), 1U);
  EXPECT_EQ(decoded.journal.events[0].sections.size(), 9U);
  const auto &typed = decoded.journal.events[0];
  EXPECT_EQ(typed.core.duration_ns, event->update.duration_ns);
  EXPECT_EQ(typed.core.terminal_status,
            ov_msckf::CP2UpdateTerminalStatus::kCommittedCounted);
  EXPECT_EQ(typed.core.terminal_subreason,
            ov_msckf::CP2UpdateTerminalSubreason::kNone);
  EXPECT_TRUE(typed.core.shadow_math_completed);
  EXPECT_TRUE(typed.core.nullspace_proposal_available);
  EXPECT_TRUE(typed.core.schur_proposal_available);
  EXPECT_EQ(typed.baseline_accepted_ids,
            event->update.baseline_accepted_ids);
  EXPECT_EQ(typed.candidate_accepted_ids,
            event->update.shadow.result.schur.accepted_ids);
  EXPECT_EQ(typed.global.nullspace_gamma_status,
            ov_msckf::CP2GammaStatus::kAvailable);
  EXPECT_DOUBLE_EQ(typed.global.nullspace_retained_gamma, 2.25);
  EXPECT_EQ(typed.global.nullspace_preview.status,
            ov_msckf::MSCKFUpdatePreviewStatus::kAccepted);
  EXPECT_EQ(typed.commit_oracle.status,
            ov_msckf::CP2CommitOracleStatus::kComplete);
  EXPECT_TRUE(typed.commit_oracle.passed);
  ASSERT_EQ(typed.feature_summaries.size(), 1U);
  EXPECT_EQ(typed.feature_summaries[0].feature_id, 55U);
  EXPECT_EQ(typed.feature_summaries[0].raw_rows, 1U);
  EXPECT_TRUE(typed.feature_summaries[0].raw_layout_valid);

  const auto states = ov_msckf::CP2StateTraceCodec::DecodeStateFile(
      decoded.journal.state_file_bytes);
  const auto raw = ov_msckf::CP2TraceCodec::DecodeRawSystemFile(
      decoded.journal.raw_system_file_bytes);
  const auto proposals = ov_msckf::CP2TraceCodec::DecodeProposalFile(
      decoded.journal.proposal_file_bytes);
  ASSERT_EQ(states.size(), 4U);
  ASSERT_EQ(raw.size(), 1U);
  ASSERT_EQ(proposals.size(), 2U);
  for (std::size_t index = 0U; index < states.size(); ++index) {
    EXPECT_EQ(selected_payload(decoded.journal.state_file_bytes,
                               states[index].payload),
              event->state_phases[index].payload);
  }
  EXPECT_EQ(selected_payload(decoded.journal.raw_system_file_bytes,
                             raw[0].payload),
            event->raw_system_payloads[0]);
  EXPECT_EQ(selected_payload(decoded.journal.proposal_file_bytes,
                             proposals[0].payload),
            event->baseline_proposal_payload);
  EXPECT_EQ(selected_payload(decoded.journal.proposal_file_bytes,
                             proposals[1].payload),
            event->candidate_proposal_payload);
  EXPECT_EQ(ov_msckf::CP2StateTraceCodec::EncodeStateFile(states).bytes,
            decoded.journal.state_file_bytes);
  EXPECT_EQ(ov_msckf::CP2TraceCodec::EncodeRawSystemFile(raw).bytes,
            decoded.journal.raw_system_file_bytes);
  EXPECT_EQ(ov_msckf::CP2TraceCodec::EncodeProposalFile(proposals).bytes,
            decoded.journal.proposal_file_bytes);
}

TEST(CP2TraceJournalPayloads, NonzeroRawAndCommittedDecodeAccepted) {
  for (const auto &event :
       {make_raw_rejected_event(), make_committed_event()}) {
    auto writer = std::make_shared<MemoryWriter>();
    CP2TraceJournalSink sink(writer);
    ASSERT_EQ(sink.Publish(event), CP2RecordedSinkStatus::kPublished);
    const auto decoded =
        ov_msckf::CP2TraceJournalCodec::Decode(writer->bytes);
    ASSERT_TRUE(decoded.accepted())
        << ov_msckf::cp2_trace_journal_decode_status_name(decoded.status);
    ASSERT_EQ(decoded.journal.events.size(), 1U);
    EXPECT_EQ(decoded.journal.events[0].raw_system_count, 1U);
  }
}

TEST(CP2TraceJournalPayloads, AnyPayloadByteMismatchRejectsBeforeWrite) {
  auto writer = std::make_shared<MemoryWriter>();
  CP2TraceJournalSink sink(writer);
  auto event = make_committed_event();
  ASSERT_FALSE(event->raw_system_payloads[0].empty());
  event->raw_system_payloads[0].back() ^= UINT8_C(1);
  const std::vector<std::uint8_t> before = writer->bytes;
  EXPECT_EQ(sink.Publish(event), CP2RecordedSinkStatus::kRejected);
  EXPECT_EQ(sink.failure(), CP2TraceJournalFailure::kRecordInvariant);
  EXPECT_EQ(writer->bytes, before);
}

TEST(CP2TraceJournalDecoder, HeaderBootstrapAndLimitCorruptionsReject) {
  auto writer = std::make_shared<MemoryWriter>();
  CP2TraceJournalSink sink(writer);
  ASSERT_EQ(sink.Publish(make_zero_event(0U, 1U, 2U)),
            CP2RecordedSinkStatus::kPublished);
  const std::vector<std::uint8_t> valid = writer->bytes;
  const FrameLocation bootstrap = locate_frame(valid, 32U);

  std::vector<std::uint8_t> corrupted = valid;
  corrupted[0] ^= UINT8_C(1);
  expect_decode_status(corrupted, CP2TraceJournalDecodeStatus::kInvalidHeader);

  corrupted = valid;
  store_u64(corrupted, bootstrap.version, 2U);
  expect_decode_status(corrupted, CP2TraceJournalDecodeStatus::kInvalidVersion);

  corrupted = valid;
  store_u64(corrupted, bootstrap.sections[0].tag, 102U);
  expect_decode_status(corrupted,
                       CP2TraceJournalDecodeStatus::kInvalidBootstrap);

  corrupted = valid;
  corrupted[bootstrap.sections[0].data] ^= UINT8_C(1);
  expect_decode_status(corrupted,
                       CP2TraceJournalDecodeStatus::kInvalidBootstrap);

  ov_msckf::CP2TraceJournalDecodeLimits limits;
  limits.maximum_total_bytes = valid.size() - 1U;
  EXPECT_EQ(ov_msckf::CP2TraceJournalCodec::Decode(valid, limits).status,
            CP2TraceJournalDecodeStatus::kTotalBytesLimit);
  limits = ov_msckf::CP2TraceJournalDecodeLimits{};
  limits.maximum_sections_per_frame = 2U;
  EXPECT_EQ(ov_msckf::CP2TraceJournalCodec::Decode(valid, limits).status,
            CP2TraceJournalDecodeStatus::kSectionLimit);
  limits = ov_msckf::CP2TraceJournalDecodeLimits{};
  limits.maximum_section_bytes = 31U;
  EXPECT_EQ(ov_msckf::CP2TraceJournalCodec::Decode(valid, limits).status,
            CP2TraceJournalDecodeStatus::kSectionBytesLimit);
  limits = ov_msckf::CP2TraceJournalDecodeLimits{};
  limits.maximum_frames = 0U;
  EXPECT_EQ(ov_msckf::CP2TraceJournalCodec::Decode(valid, limits).status,
            CP2TraceJournalDecodeStatus::kFrameLimit);
}

TEST(CP2TraceJournalDecoder,
     TruncationTrailingAndHostileSectionPopulationReject) {
  auto writer = std::make_shared<MemoryWriter>();
  CP2TraceJournalSink sink(writer);
  ASSERT_EQ(sink.Publish(make_zero_event(0U, 1U, 2U)),
            CP2RecordedSinkStatus::kPublished);
  const std::vector<std::uint8_t> valid = writer->bytes;
  const FrameLocation event = locate_frame(valid, known_preamble().size());

  std::vector<std::uint8_t> corrupted = valid;
  corrupted.pop_back();
  expect_decode_status(corrupted,
                       CP2TraceJournalDecodeStatus::kTruncatedFrame);

  corrupted = valid;
  corrupted.push_back(0U);
  expect_decode_status(corrupted,
                       CP2TraceJournalDecodeStatus::kTrailingBytes);

  corrupted = valid;
  store_u64(corrupted, event.section_count, UINT64_C(1000010));
  expect_decode_status(corrupted,
                       CP2TraceJournalDecodeStatus::kTruncatedFrame);

  corrupted = valid;
  const std::uint64_t remaining =
      event.end - event.sections[0].data;
  store_u64(corrupted, event.sections[0].length, remaining + 1U);
  expect_decode_status(corrupted,
                       CP2TraceJournalDecodeStatus::kTruncatedFrame);

  corrupted = valid;
  store_u64(corrupted, event.sections[0].length, UINT64_MAX);
  expect_decode_status(corrupted,
                       CP2TraceJournalDecodeStatus::kSectionBytesLimit);
}

TEST(CP2TraceJournalDecoder,
     DuplicateUnknownMissingAndInvalidCoreSectionsReject) {
  auto writer = std::make_shared<MemoryWriter>();
  CP2TraceJournalSink sink(writer);
  ASSERT_EQ(sink.Publish(make_zero_event(0U, 1U, 2U)),
            CP2RecordedSinkStatus::kPublished);
  const std::vector<std::uint8_t> valid = writer->bytes;
  const std::size_t event_begin = known_preamble().size();
  const FrameLocation event = locate_frame(valid, event_begin);

  std::vector<std::uint8_t> corrupted = valid;
  store_u64(corrupted, event.sections[1].tag, 1U);
  expect_decode_status(
      corrupted, CP2TraceJournalDecodeStatus::kInvalidSectionInventory);

  corrupted = valid;
  store_u64(corrupted, event.sections[7].tag, 9U);
  expect_decode_status(
      corrupted, CP2TraceJournalDecodeStatus::kInvalidSectionInventory);

  corrupted.assign(valid.begin(),
                   valid.begin() +
                       static_cast<std::ptrdiff_t>(event.sections[7].tag));
  store_u64(corrupted, event.section_count, 7U);
  store_u64(corrupted, event.body_length,
            corrupted.size() - event_begin - 8U);
  expect_decode_status(
      corrupted, CP2TraceJournalDecodeStatus::kInvalidSectionInventory);

  corrupted = valid;
  const std::uint64_t flags =
      load_u64(corrupted, event.sections[0].data + 9U * 8U);
  store_u64(corrupted, event.sections[0].data + 9U * 8U,
            flags | (UINT64_C(1) << 63U));
  expect_decode_status(corrupted,
                       CP2TraceJournalDecodeStatus::kInvalidSectionValue);

  corrupted = valid;
  corrupted.erase(
      corrupted.begin() + static_cast<std::ptrdiff_t>(
                              event.sections[0].data + 192U),
      corrupted.begin() + static_cast<std::ptrdiff_t>(
                              event.sections[0].data + 200U));
  store_u64(corrupted, event.sections[0].length, 192U);
  store_u64(corrupted, event.body_length,
            load_u64(valid, event.body_length) - 8U);
  expect_decode_status(corrupted,
                       CP2TraceJournalDecodeStatus::kInvalidSectionValue);
}

TEST(CP2TraceJournalDecoder,
     CanonicalFragmentAndCrossIdentityCorruptionsReject) {
  auto writer = std::make_shared<MemoryWriter>();
  CP2TraceJournalSink sink(writer);
  ASSERT_EQ(sink.Publish(make_raw_rejected_event()),
            CP2RecordedSinkStatus::kPublished);
  const std::vector<std::uint8_t> valid = writer->bytes;
  const FrameLocation event = locate_frame(valid, known_preamble().size());
  ASSERT_EQ(event.sections.size(), 9U);
  ASSERT_GT(event.sections[6].size, 48U);

  std::vector<std::uint8_t> corrupted = valid;
  corrupted[event.sections[6].data + 48U] ^= UINT8_C(1);
  expect_decode_status(
      corrupted, CP2TraceJournalDecodeStatus::kCanonicalCodecFailure);

  corrupted = valid;
  store_u64(corrupted, event.sections[6].data, 8U);
  expect_decode_status(corrupted,
                       CP2TraceJournalDecodeStatus::kIdentityViolation);

  corrupted = valid;
  store_u64(corrupted, event.sections[8].data + 2U * 8U, 2U);
  expect_decode_status(corrupted,
                       CP2TraceJournalDecodeStatus::kInvalidSectionValue);

  auto committed_writer = std::make_shared<MemoryWriter>();
  CP2TraceJournalSink committed_sink(committed_writer);
  ASSERT_EQ(committed_sink.Publish(make_committed_event()),
            CP2RecordedSinkStatus::kPublished);
  corrupted = committed_writer->bytes;
  const FrameLocation committed =
      locate_frame(corrupted, known_preamble().size());
  ASSERT_EQ(committed.sections[1].size, 8U);
  store_u64(corrupted, committed.sections[1].data, 999U);
  expect_decode_status(corrupted,
                       CP2TraceJournalDecodeStatus::kInvalidSectionValue);

  auto ordered_writer = std::make_shared<MemoryWriter>();
  CP2TraceJournalSink ordered_sink(ordered_writer);
  ASSERT_EQ(ordered_sink.Publish(make_two_raw_committed_event()),
            CP2RecordedSinkStatus::kPublished);
  corrupted = ordered_writer->bytes;
  const FrameLocation ordered =
      locate_frame(corrupted, known_preamble().size());
  ASSERT_EQ(ordered.sections[1].size, 16U);
  const std::uint64_t first_id =
      load_u64(corrupted, ordered.sections[1].data);
  const std::uint64_t second_id =
      load_u64(corrupted, ordered.sections[1].data + 8U);
  store_u64(corrupted, ordered.sections[1].data, second_id);
  store_u64(corrupted, ordered.sections[1].data + 8U, first_id);
  expect_decode_status(corrupted,
                       CP2TraceJournalDecodeStatus::kInvalidSectionValue);
}

TEST(CP2TraceJournalFile, PreopenedRegularFileFinalizesAtExactSize) {
  TemporaryFile file;
  ASSERT_GE(file.descriptor, 0);
  std::shared_ptr<CP2TraceJournalSink> sink;
  ASSERT_TRUE(CP2TraceJournalSink::CreateForPreopenedFile(file.descriptor,
                                                         sink));
  ASSERT_TRUE(sink);
  ASSERT_EQ(sink->Publish(make_zero_event(0U, 1U, 2U)),
            CP2RecordedSinkStatus::kPublished);
  ASSERT_TRUE(sink->Finalize());
  struct stat status {};
  ASSERT_EQ(::fstat(file.descriptor, &status), 0);
  EXPECT_EQ(static_cast<std::uint64_t>(status.st_size),
            sink->bytes_written());
  const off_t offset = ::lseek(file.descriptor, 0, SEEK_CUR);
  ASSERT_GE(offset, 0);
  EXPECT_EQ(static_cast<std::uint64_t>(offset), sink->bytes_written());
}

TEST(CP2TraceJournalFile, ReadOnlyAndMultipleLinkFilesReject) {
  TemporaryFile file;
  ASSERT_GE(file.descriptor, 0);
  const std::string link_path = file.path + ".link";
  ASSERT_EQ(::link(file.path.c_str(), link_path.c_str()), 0);
  std::shared_ptr<CP2TraceJournalSink> sink;
  EXPECT_FALSE(CP2TraceJournalSink::CreateForPreopenedFile(file.descriptor,
                                                          sink));
  EXPECT_FALSE(sink);
  EXPECT_EQ(::unlink(link_path.c_str()), 0);

  const int readonly = ::open(file.path.c_str(), O_RDONLY | O_CLOEXEC);
  ASSERT_GE(readonly, 0);
  EXPECT_FALSE(
      CP2TraceJournalSink::CreateForPreopenedFile(readonly, sink));
  EXPECT_FALSE(sink);
  EXPECT_EQ(::close(readonly), 0);
}

TEST(CP2TraceJournalFile, CallerOffsetInterferenceFailsClosed) {
  TemporaryFile file;
  ASSERT_GE(file.descriptor, 0);
  std::shared_ptr<CP2TraceJournalSink> sink;
  ASSERT_TRUE(CP2TraceJournalSink::CreateForPreopenedFile(file.descriptor,
                                                         sink));
  ASSERT_TRUE(sink);
  ASSERT_EQ(::lseek(file.descriptor, 0, SEEK_SET), 0);
  EXPECT_EQ(sink->Publish(make_zero_event(0U, 1U, 2U)),
            CP2RecordedSinkStatus::kRejected);
  EXPECT_EQ(sink->failure(), CP2TraceJournalFailure::kWriteFailure);
  EXPECT_FALSE(sink->Finalize());
}

} // namespace
