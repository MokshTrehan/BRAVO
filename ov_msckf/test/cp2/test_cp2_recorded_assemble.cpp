/*
 * SchurVIO-Lite CP2 recorded assembler tests.
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include "update/CP2Canonical.h"
#include "update/CP2StateTraceCodec.h"
#include "update/CP2TraceCodec.h"
#include "update/CP2TraceJournal.h"

#include <gtest/gtest.h>

#include <algorithm>
#include <array>
#include <cerrno>
#include <cfenv>
#include <chrono>
#include <csignal>
#include <cstddef>
#include <cstdint>
#include <dirent.h>
#include <fcntl.h>
#include <memory>
#include <poll.h>
#include <string>
#include <sys/prctl.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>
#include <vector>

namespace ov_msckf {
int cp2_recorded_assemble_entry(int argc, char **argv) noexcept;
}

namespace {

constexpr int kHarnessCleanupFailureExit = 90;
constexpr int kHarnessParentDeathContractExit = 91;
constexpr int kHarnessDiagnosticRedirectExit = 92;
constexpr int kHarnessRoundingSetupExit = 93;

class MemoryWriter final : public ov_msckf::CP2TraceJournalWriter {
public:
  ov_msckf::CP2TraceJournalWriteResult
  Write(const std::uint8_t *data, std::size_t size) noexcept override {
    try {
      bytes.insert(bytes.end(), data, data + size);
      return {true, size};
    } catch (...) {
      return {false, 0U};
    }
  }
  bool Sync(std::uint64_t expected) noexcept override {
    return expected == bytes.size();
  }
  std::vector<std::uint8_t> bytes;
};

void write_bytes(const std::string &path,
                 const std::vector<std::uint8_t> &bytes) {
  const int descriptor = ::open(path.c_str(), O_WRONLY | O_CREAT | O_EXCL |
                                                  O_CLOEXEC | O_NOFOLLOW,
                                0600);
  ASSERT_GE(descriptor, 0);
  std::size_t offset = 0U;
  while (offset < bytes.size()) {
    const ssize_t amount =
        ::write(descriptor, bytes.data() + offset, bytes.size() - offset);
    ASSERT_GT(amount, 0);
    offset += static_cast<std::size_t>(amount);
  }
  ASSERT_EQ(::close(descriptor), 0);
}

void write_text(const std::string &path, const std::string &text) {
  write_bytes(path, std::vector<std::uint8_t>(text.begin(), text.end()));
}

std::string read_text(const std::string &path) {
  const int descriptor = ::open(path.c_str(), O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
  EXPECT_GE(descriptor, 0);
  std::string result;
  std::array<char, 4096> buffer{};
  while (descriptor >= 0) {
    const ssize_t amount = ::read(descriptor, buffer.data(), buffer.size());
    if (amount == 0) break;
    EXPECT_GT(amount, 0);
    if (amount < 0) break;
    result.append(buffer.data(), static_cast<std::size_t>(amount));
  }
  if (descriptor >= 0) {
    EXPECT_EQ(::close(descriptor), 0);
  }
  return result;
}

void remove_tree(const std::string &path) {
  DIR *directory = ::opendir(path.c_str());
  if (directory == nullptr) {
    (void)::unlink(path.c_str());
    return;
  }
  while (dirent *entry = ::readdir(directory)) {
    const std::string name(entry->d_name);
    if (name == "." || name == "..") continue;
    const std::string child = path + "/" + name;
    struct stat status {};
    if (::lstat(child.c_str(), &status) == 0 && S_ISDIR(status.st_mode))
      remove_tree(child);
    else
      (void)::unlink(child.c_str());
  }
  (void)::closedir(directory);
  (void)::rmdir(path.c_str());
}

class TemporaryDirectory {
public:
  TemporaryDirectory() {
    std::array<char, 64> value{};
    const char pattern[] = "/tmp/cp2-recorded-assemble-XXXXXX";
    std::copy(pattern, pattern + sizeof(pattern), value.begin());
    char *created = ::mkdtemp(value.data());
    if (created != nullptr) path = created;
  }
  ~TemporaryDirectory() { if (!path.empty()) remove_tree(path); }
  std::string path;
};

int invoke_assembler(const std::string &spec, const std::string &output) {
  const std::string executable("cp2_recorded_assemble");
  const std::string spec_flag("--spec");
  const std::string output_flag("--output-dir");
  char *arguments[] = {
      const_cast<char *>(executable.c_str()),
      const_cast<char *>(spec_flag.c_str()), const_cast<char *>(spec.c_str()),
      const_cast<char *>(output_flag.c_str()),
      const_cast<char *>(output.c_str()), nullptr};
  return ov_msckf::cp2_recorded_assemble_entry(5, arguments);
}

bool process_is_single_threaded() {
  DIR *tasks = ::opendir("/proc/self/task");
  if (tasks == nullptr) return false;
  std::size_t count = 0U;
  errno = 0;
  while (dirent *entry = ::readdir(tasks)) {
    const std::string name(entry->d_name);
    if (name != "." && name != "..") ++count;
  }
  const bool complete = errno == 0;
  const bool closed = ::closedir(tasks) == 0;
  return complete && closed && count == 1U;
}

bool child_process_contract_ready() {
  if (!process_is_single_threaded()) return false;
  struct sigaction action {};
  if (::sigaction(SIGCHLD, nullptr, &action) != 0) return false;
  return action.sa_handler == SIG_DFL && (action.sa_flags & SA_NOCLDWAIT) == 0;
}

bool wait_interval() {
  const int result = ::poll(nullptr, 0, 10);
  return result == 0 || (result < 0 && errno == EINTR);
}

bool terminate_and_reap(pid_t child) {
  if (::kill(child, SIGKILL) != 0 && errno != ESRCH) return false;
  const auto deadline =
      std::chrono::steady_clock::now() + std::chrono::seconds(5);
  for (;;) {
    const pid_t waited = ::waitpid(child, nullptr, WNOHANG);
    if (waited == child || (waited < 0 && errno == ECHILD)) return true;
    if (waited < 0 && errno == EINTR) {
      if (std::chrono::steady_clock::now() >= deadline) return false;
      continue;
    }
    if (waited < 0 || std::chrono::steady_clock::now() >= deadline)
      return false;
    if (!wait_interval()) return false;
  }
}

void terminate_or_fail_stop(pid_t child) {
  if (!terminate_and_reap(child)) _exit(kHarnessCleanupFailureExit);
}

bool wait_for_child(pid_t child, int &status) {
  const auto deadline =
      std::chrono::steady_clock::now() + std::chrono::seconds(30);
  for (;;) {
    if (std::chrono::steady_clock::now() >= deadline) {
      terminate_or_fail_stop(child);
      return false;
    }
    const pid_t waited = ::waitpid(child, &status, WNOHANG);
    if (waited == child)
      return std::chrono::steady_clock::now() < deadline;
    if (waited < 0 && errno == EINTR) {
      if (std::chrono::steady_clock::now() >= deadline) {
        terminate_or_fail_stop(child);
        return false;
      }
      continue;
    }
    if (waited < 0 && errno == ECHILD) return false;
    if (waited < 0) {
      terminate_or_fail_stop(child);
      return false;
    }
    if (!wait_interval()) {
      terminate_or_fail_stop(child);
      return false;
    }
  }
}

int anonymous_diagnostic_file() {
  std::array<char, 64> path{};
  const char pattern[] = "/tmp/cp2-recorded-stderr-XXXXXX";
  std::copy(pattern, pattern + sizeof(pattern), path.begin());
  const int descriptor = ::mkstemp(path.data());
  if (descriptor < 0) return -1;
  const bool unlinked = ::unlink(path.data()) == 0;
  struct stat status {};
  const bool valid = unlinked && ::fstat(descriptor, &status) == 0 &&
                     S_ISREG(status.st_mode) && status.st_nlink == 0 &&
                     (status.st_mode & 0777) == 0600;
  if (!valid) {
    (void)::close(descriptor);
    if (!unlinked) (void)::unlink(path.data());
    return -1;
  }
  return descriptor;
}

bool read_diagnostic(int descriptor, std::string &diagnostic) {
  constexpr std::size_t maximum_size = 65536U;
  struct stat status {};
  if (::fstat(descriptor, &status) != 0 || !S_ISREG(status.st_mode) ||
      status.st_nlink != 0 || status.st_size < 0 ||
      static_cast<std::uintmax_t>(status.st_size) > maximum_size) {
    return false;
  }
  diagnostic.assign(static_cast<std::size_t>(status.st_size), '\0');
  std::size_t offset = 0U;
  while (offset < diagnostic.size()) {
    const ssize_t amount =
        ::pread(descriptor, &diagnostic[offset], diagnostic.size() - offset,
                static_cast<off_t>(offset));
    if (amount < 0 && errno == EINTR) continue;
    if (amount <= 0) return false;
    offset += static_cast<std::size_t>(amount);
  }
  struct stat final_status {};
  return ::fstat(descriptor, &final_status) == 0 &&
         final_status.st_dev == status.st_dev &&
         final_status.st_ino == status.st_ino &&
         final_status.st_size == status.st_size && final_status.st_nlink == 0;
}

int run_assembler(const std::string &spec, const std::string &output,
                  const std::string &expected_diagnostic = std::string(),
                  int child_rounding = FE_TONEAREST) {
  if (!child_process_contract_ready() ||
      std::fegetround() != FE_TONEAREST) return -1;
  const int diagnostic = anonymous_diagnostic_file();
  if (diagnostic < 0) return -1;
  if (!child_process_contract_ready() ||
      std::fegetround() != FE_TONEAREST) {
    (void)::close(diagnostic);
    return -1;
  }
  const pid_t parent = ::getpid();
  const pid_t child = ::fork();
  if (child == 0) {
    if (::prctl(PR_SET_PDEATHSIG, SIGKILL) != 0 || ::getppid() != parent)
      _exit(kHarnessParentDeathContractExit);
    if (::dup2(diagnostic, STDERR_FILENO) < 0)
      _exit(kHarnessDiagnosticRedirectExit);
    if (diagnostic != STDERR_FILENO) (void)::close(diagnostic);
    if (std::fesetround(child_rounding) != 0 ||
        std::fegetround() != child_rounding) {
      _exit(kHarnessRoundingSetupExit);
    }
    _exit(invoke_assembler(spec, output));
  }
  if (child < 0) {
    (void)::close(diagnostic);
    return -1;
  }
  int status = 0;
  if (!wait_for_child(child, status)) {
    (void)::close(diagnostic);
    return -1;
  }
  std::string observed_diagnostic;
  const bool diagnostic_valid = read_diagnostic(diagnostic, observed_diagnostic);
  const bool diagnostic_closed = ::close(diagnostic) == 0;
  if (!diagnostic_valid || !diagnostic_closed ||
      std::fegetround() != FE_TONEAREST ||
      observed_diagnostic != expected_diagnostic) {
    return -1;
  }
  return WIFEXITED(status) ? WEXITSTATUS(status) : -1;
}

std::uint64_t load_u64(const std::vector<std::uint8_t> &bytes,
                       std::size_t offset) {
  if (offset > bytes.size() || bytes.size() - offset < 8U)
    throw std::runtime_error("test u64 is out of range");
  std::uint64_t value = 0U;
  for (std::size_t index = 0U; index < 8U; ++index)
    value = (value << 8U) | bytes[offset + index];
  return value;
}

void store_u64(std::vector<std::uint8_t> &bytes, std::size_t offset,
               std::uint64_t value) {
  if (offset > bytes.size() || bytes.size() - offset < 8U)
    throw std::runtime_error("test u64 is out of range");
  for (std::size_t index = 0U; index < 8U; ++index)
    bytes[offset + index] =
        static_cast<std::uint8_t>(value >> (56U - 8U * index));
}

std::vector<std::uint8_t> zero_event_journal() {
  auto writer = std::make_shared<MemoryWriter>();
  ov_msckf::CP2TraceJournalSink sink(writer);
  auto event = std::make_shared<ov_msckf::CP2RecordedUpdateEvent>();
  event->update.invocation_context_available = true;
  event->update.sequence_index = 0U;
  event->update.pair_index = 0U;
  event->update.camera_timestamp_ns = 100U;
  event->update.invocation_id = 0U;
  event->update.timing_endpoint_valid = true;
  event->update.timing_start_ns = 1000U;
  event->update.timing_end_ns = 1001U;
  event->update.duration_ns = 1U;
  event->update.terminal_status =
      ov_msckf::CP2UpdateTerminalStatus::kEmptyInput;
  event->update.terminal_subreason =
      ov_msckf::CP2UpdateTerminalSubreason::kInputEmpty;
  event->online_math_evidence_passed = true;
  EXPECT_EQ(sink.Publish(event), ov_msckf::CP2RecordedSinkStatus::kPublished);
  EXPECT_TRUE(sink.Finalize());
  return writer->bytes;
}

void install_empty_phase(ov_msckf::CP2RecordedUpdateEvent &event,
                         std::size_t index, ov_msckf::CP2StatePhase phase) {
  ov_msckf::CP2CompositeStateSnapshot snapshot;
  snapshot.phase = phase;
  snapshot.timestamp = 1.0;
  snapshot.covariance.resize(0, 0);
  snapshot.time_offset_nominal.resize(0, 0);
  snapshot.time_offset_fej.resize(0, 0);
  event.state_phases[index].phase = phase;
  event.state_phases[index].payload =
      ov_msckf::CP2StateTraceCodec::EncodeSnapshotPayload(snapshot);
  event.state_phases[index].snapshot.reset(
      new ov_msckf::CP2CompositeStateSnapshot(std::move(snapshot)));
}

std::vector<std::uint8_t> one_gate_event_journal(
    bool install_failed_statistics = false) {
  auto writer = std::make_shared<MemoryWriter>();
  ov_msckf::CP2TraceJournalSink sink(writer);
  auto event = std::make_shared<ov_msckf::CP2RecordedUpdateEvent>();
  event->update.invocation_context_available = true;
  event->update.sequence_index = 0U;
  event->update.pair_index = 0U;
  event->update.camera_timestamp_ns = 100U;
  event->update.invocation_id = 0U;
  event->update.timing_endpoint_valid = true;
  event->update.timing_start_ns = 1000U;
  event->update.timing_end_ns = 1001U;
  event->update.duration_ns = 1U;
  event->update.terminal_status =
      ov_msckf::CP2UpdateTerminalStatus::kAllRejected;
  event->update.terminal_subreason =
      ov_msckf::CP2UpdateTerminalSubreason::kAllBaselineFeaturesRejected;
  event->update.input_feature_count = 1U;
  event->update.raw_system_count = 1U;
  event->update.baseline_gamma_status = ov_msckf::CP2GammaStatus::kAvailable;
  event->update.baseline_gamma = 0.0;
  event->update.shadow_evidence_available = true;
  event->update.shadow.shadow_math_completed = true;
  event->update.shadow.result.traversal_complete = true;
  event->update.shadow.result.raw_layouts_valid = true;
  event->update.shadow.result.nullspace_assembly_valid = true;
  event->update.shadow.result.schur_assembly_valid = true;
  event->update.shadow.result.input_valid = true;
  event->update.shadow.result.nullspace.gamma_status =
      ov_msckf::CP2GammaStatus::kAvailable;
  event->update.shadow.result.nullspace.retained_gamma = 0.0;
  event->update.shadow.result.schur.gamma_status =
      ov_msckf::CP2GammaStatus::kAvailable;
  event->update.shadow.result.schur.retained_gamma = 0.0;
  ov_msckf::CP2RawFeatureSystem raw;
  raw.feature_id = 7U;
  raw.H_x.resize(1, 1);
  raw.H_x << 0.0;
  raw.H_f.resize(1, 3);
  raw.H_f << 1.0, 2.0, 3.0;
  raw.residual.resize(1);
  raw.residual << 4.0;
  raw.jacobian_layout.emplace_back(0, 1, 0);
  event->update.shadow.input.raw_systems.push_back(raw);
  ov_msckf::CP2FeaturePairResult feature;
  feature.feature_id = raw.feature_id;
  feature.raw_rows = 1;
  feature.raw_layout_valid = true;
  feature.nullspace.raw_rows = 1;
  feature.schur.raw_rows = 1;
  feature.nullspace_gate.stage = ov_msckf::CP2FeatureGateStage::kDecision;
  feature.nullspace_gate.evidence_decision_available = true;
  feature.schur_gate.stage = ov_msckf::CP2FeatureGateStage::kDecision;
  feature.schur_gate.evidence_decision_available = true;
  feature.agreement_class = ov_msckf::CP2GateAgreementClass::kBothMatchReject;
  feature.raw_row_match_weight = 1U;
  if (install_failed_statistics) {
    feature.nullspace.status =
        ov_msckf::CP2NullspaceReductionStatus::kAccepted;
    feature.nullspace.stage =
        ov_msckf::CP2NullspaceReductionStage::kAccepted;
    feature.schur.status = ov_msckf::SchurReductionStatus::kAccepted;
    feature.schur.stage = ov_msckf::SchurReductionStage::kAccepted;
    feature.schur.raw_lambda_symmetry_error_inf = 0.0;
    feature.schur.gamma = 0.0;
    feature.statistics_comparison_required = true;
    const auto comparison = [](double error, bool passed) {
      ov_msckf::CP2StatisticComparison value;
      value.status = ov_msckf::CP2StatisticComparisonStatus::kAvailable;
      value.available = true;
      value.reference_norm = 0.0;
      value.error = error;
      value.tolerance = 1.0e-10;
      value.ratio = error / value.tolerance;
      value.passed = passed;
      return value;
    };
    feature.lambda_comparison = comparison(1.0, false);
    feature.eta_comparison = comparison(0.0, true);
    feature.gamma_comparison = comparison(0.0, true);
  }
  event->update.shadow.result.features.push_back(feature);
  event->raw_system_payloads.push_back(
      ov_msckf::CP2TraceCodec::EncodeRawSystemPayload(raw));
  install_empty_phase(*event, 0U, ov_msckf::CP2StatePhase::kPhase0Prior);
  install_empty_phase(*event, 1U,
                      ov_msckf::CP2StatePhase::kPhase1Precommit);
  event->state_phase_count = 2U;
  event->phase01_canonical_equal = true;
  event->phase01_pointer_graph_equal = true;
  event->online_math_evidence_passed = true;
  EXPECT_EQ(sink.Publish(event), ov_msckf::CP2RecordedSinkStatus::kPublished);
  EXPECT_TRUE(sink.Finalize());
  return writer->bytes;
}

std::vector<std::uint8_t> inject_fragment(
    std::vector<std::uint8_t> journal, std::uint64_t selected_tag,
    const std::vector<std::uint8_t> &fragment) {
  const std::size_t bootstrap = 32U;
  const std::size_t event_begin =
      bootstrap + 8U + static_cast<std::size_t>(load_u64(journal, bootstrap));
  const std::size_t section_count_offset = event_begin + 16U;
  const std::uint64_t section_count = load_u64(journal, section_count_offset);
  std::size_t cursor = event_begin + 24U;
  for (std::uint64_t index = 0U; index < section_count; ++index) {
    const std::uint64_t tag = load_u64(journal, cursor);
    const std::size_t length_offset = cursor + 8U;
    const std::size_t data_offset = cursor + 16U;
    const std::uint64_t length = load_u64(journal, length_offset);
    if (tag == selected_tag) {
      EXPECT_EQ(length, 0U);
      journal.insert(journal.begin() + static_cast<std::ptrdiff_t>(data_offset),
                     fragment.begin(), fragment.end());
      store_u64(journal, length_offset,
                static_cast<std::uint64_t>(fragment.size()));
      store_u64(journal, event_begin,
                load_u64(journal, event_begin) +
                    static_cast<std::uint64_t>(fragment.size()));
      return journal;
    }
    cursor = data_offset + static_cast<std::size_t>(length);
  }
  throw std::runtime_error("test journal lacks selected fragment tag");
}

std::vector<std::uint8_t> state_fragment() {
  std::vector<ov_msckf::CP2StateTraceFrame> frames(2U);
  for (std::size_t index = 0U; index < frames.size(); ++index) {
    auto &frame = frames[index];
    frame.invocation = {0U, 0U, 0U};
    frame.phase = index == 0U
                      ? ov_msckf::CP2StatePhase::kPhase0Prior
                      : ov_msckf::CP2StatePhase::kPhase1Precommit;
    frame.snapshot.phase = frame.phase;
    frame.snapshot.covariance.resize(0, 0);
    frame.snapshot.time_offset_nominal.resize(0, 0);
    frame.snapshot.time_offset_fej.resize(0, 0);
  }
  const auto file = ov_msckf::CP2StateTraceCodec::EncodeStateFile(frames);
  return std::vector<std::uint8_t>(file.bytes.begin() + 32, file.bytes.end());
}

std::vector<std::uint8_t> proposal_fragment() {
  ov_msckf::CP2ProposalTraceFrame frame;
  frame.invocation = {0U, 0U, 0U};
  frame.role = ov_msckf::CP2ProposalTraceRole::kNullspaceBaseline;
  frame.proposal.dx.resize(1);
  frame.proposal.dx << 0.0;
  frame.proposal.P_plus.resize(1, 1);
  frame.proposal.P_plus << 1.0;
  const auto file = ov_msckf::CP2TraceCodec::EncodeProposalFile({frame});
  return std::vector<std::uint8_t>(file.bytes.begin() + 32, file.bytes.end());
}

std::vector<std::uint8_t> raw_fragment() {
  ov_msckf::CP2RawSystemTraceFrame frame;
  frame.invocation = {0U, 0U, 0U};
  frame.feature_ordinal = 0U;
  frame.raw_system.feature_id = 7U;
  frame.raw_system.H_x.resize(1, 0);
  frame.raw_system.H_f.resize(1, 3);
  frame.raw_system.H_f << 1.0, 2.0, 3.0;
  frame.raw_system.residual.resize(1);
  frame.raw_system.residual << 4.0;
  const auto file = ov_msckf::CP2TraceCodec::EncodeRawSystemFile({frame});
  return std::vector<std::uint8_t>(file.bytes.begin() + 32, file.bytes.end());
}

std::string make_spec(const std::string &root,
                      const std::string &third_sequence,
                      const std::string &serial_sha) {
  const std::string zero_hash(64U, '0');
  const std::array<const char *, 3> ids{{
      "MH_01_easy", "MH_03_medium", third_sequence.c_str()}};
  std::string runs = "[";
  for (std::size_t index = 0U; index < ids.size(); ++index) {
    if (index != 0U) runs += ",";
    runs += "{\"sequence_index\":" + std::to_string(index) +
            ",\"sequence_id\":\"" + ids[index] +
            "\",\"bag_sha256\":\"" + zero_hash +
            "\",\"resolved_parameters_sha256\":\"" + zero_hash +
            "\",\"journal\":\"" + root + "/journal" +
            std::to_string(index) + "\"}";
  }
  runs += "]";
  return "{\"schema_version\":1,\"record_type\":"
         "\"cp2_recorded_assembly_spec\",\"config_sha256\":\"" +
         zero_hash + "\",\"serial_pairs\":\"" + root +
         "/serial_pairs.jsonl\",\"serial_pairs_sha256\":\"" +
         serial_sha + "\",\"runs\":" + runs + "}\n";
}

TEST(CP2RecordedAssemble, EmptyCanonicalCampaignIsDeterministicAndUsesV101) {
  TemporaryDirectory temporary;
  ASSERT_FALSE(temporary.path.empty());
  ASSERT_EQ(::mkdir((temporary.path + "/output").c_str(), 0700), 0);
  auto writer = std::make_shared<MemoryWriter>();
  ov_msckf::CP2TraceJournalSink sink(writer);
  ASSERT_TRUE(sink.ready());
  ASSERT_TRUE(sink.Finalize());
  for (std::size_t index = 0U; index < 3U; ++index)
    write_bytes(temporary.path + "/journal" + std::to_string(index),
                writer->bytes);
  write_text(temporary.path + "/serial_pairs.jsonl", "");
  ov_msckf::CP2Sha256 empty_digest;
  const std::string spec = temporary.path + "/spec.json";
  write_text(spec, make_spec(temporary.path, "V1_01_easy",
                             empty_digest.HexDigest()));
  ASSERT_EQ(run_assembler(spec, temporary.path + "/output"), 0);
  const std::string summary =
      read_text(temporary.path + "/output/summary.json");
  EXPECT_NE(summary.find("\"sequence_id\":\"V1_01_easy\""),
            std::string::npos);
  EXPECT_NE(summary.find("\"attempted_updates\":0"), std::string::npos);
  EXPECT_NE(summary.find("\"passed_pre_replay\":false"),
            std::string::npos);
  EXPECT_EQ(read_text(temporary.path + "/output/updates.jsonl"), "");
}

TEST(CP2RecordedAssemble, WrongFrozenSequenceAndCorruptJournalFailClosed) {
  TemporaryDirectory wrong_sequence;
  ASSERT_FALSE(wrong_sequence.path.empty());
  ASSERT_EQ(::mkdir((wrong_sequence.path + "/output").c_str(), 0700), 0);
  auto writer = std::make_shared<MemoryWriter>();
  ov_msckf::CP2TraceJournalSink sink(writer);
  ASSERT_TRUE(sink.Finalize());
  for (std::size_t index = 0U; index < 3U; ++index)
    write_bytes(wrong_sequence.path + "/journal" + std::to_string(index),
                writer->bytes);
  write_text(wrong_sequence.path + "/serial_pairs.jsonl", "");
  ov_msckf::CP2Sha256 empty_digest;
  const std::string wrong_spec = wrong_sequence.path + "/spec.json";
  write_text(wrong_spec, make_spec(wrong_sequence.path, "V1_02_medium",
                                   empty_digest.HexDigest()));
  EXPECT_EQ(run_assembler(
                wrong_spec, wrong_sequence.path + "/output",
                "cp2_recorded_assemble: assembly run identity, hash, or path "
                "is invalid\n"),
            1);
  EXPECT_TRUE(read_text(wrong_spec).size() > 0U);

  TemporaryDirectory corrupt;
  ASSERT_FALSE(corrupt.path.empty());
  ASSERT_EQ(::mkdir((corrupt.path + "/output").c_str(), 0700), 0);
  std::vector<std::uint8_t> damaged = writer->bytes;
  ASSERT_FALSE(damaged.empty());
  damaged[0] ^= UINT8_C(1);
  write_bytes(corrupt.path + "/journal0", damaged);
  write_bytes(corrupt.path + "/journal1", writer->bytes);
  write_bytes(corrupt.path + "/journal2", writer->bytes);
  write_text(corrupt.path + "/serial_pairs.jsonl", "");
  const std::string corrupt_spec = corrupt.path + "/spec.json";
  write_text(corrupt_spec, make_spec(corrupt.path, "V1_01_easy",
                                     empty_digest.HexDigest()));
  EXPECT_EQ(run_assembler(
                corrupt_spec, corrupt.path + "/output",
                "cp2_recorded_assemble: journal decode failed: invalid_header\n"),
            1);

  TemporaryDirectory skipped_invocation;
  ASSERT_FALSE(skipped_invocation.path.empty());
  ASSERT_EQ(::mkdir((skipped_invocation.path + "/output").c_str(), 0700), 0);
  for (std::size_t index = 0U; index < 3U; ++index)
    write_bytes(skipped_invocation.path + "/journal" + std::to_string(index),
                writer->bytes);
  const std::string serial =
      "{\"schema_version\":1,\"record_type\":\"serial_pair\","
      "\"sequence_index\":0,\"sequence_id\":\"MH_01_easy\","
      "\"pair_index\":0,\"anchor_filtered_index\":0,"
      "\"anchor_camera_id\":0,\"cam0_filtered_index\":0,"
      "\"cam1_filtered_index\":0,\"cam0_record_time_ns\":100,"
      "\"cam1_record_time_ns\":100,\"cam0_header_time_ns\":100,"
      "\"cam1_header_time_ns\":100,\"camera_timestamp_ns\":100,"
      "\"absolute_record_delta_ns\":0,\"selected\":true,"
      "\"enqueue_entered\":true,\"enqueue_returned\":true,"
      "\"enqueue_status\":\"queued\",\"processing_entered\":true,"
      "\"processing_returned\":true,\"processing_status\":\"processed\","
      "\"updater_invocation_ids\":[1]}\n";
  write_text(skipped_invocation.path + "/serial_pairs.jsonl", serial);
  ov_msckf::CP2Sha256 serial_digest;
  serial_digest.Update(serial);
  const std::string skipped_spec = skipped_invocation.path + "/spec.json";
  write_text(skipped_spec,
             make_spec(skipped_invocation.path, "V1_01_easy",
                       serial_digest.HexDigest()));
  EXPECT_EQ(run_assembler(
                skipped_spec, skipped_invocation.path + "/output",
                "cp2_recorded_assemble: serial updater ownership is duplicate\n"),
            1);
}

TEST(CP2RecordedAssemble, OrphanStateProposalAndRawFramesFailClosed) {
  const std::array<std::pair<std::uint64_t, std::vector<std::uint8_t>>, 3>
      corruptions{{
          {6U, state_fragment()},
          {8U, proposal_fragment()},
          {7U, raw_fragment()},
      }};
  ov_msckf::CP2Sha256 empty_digest;
  for (const auto &corruption : corruptions) {
    TemporaryDirectory temporary;
    ASSERT_FALSE(temporary.path.empty());
    ASSERT_EQ(::mkdir((temporary.path + "/output").c_str(), 0700), 0);
    write_bytes(temporary.path + "/journal0",
                inject_fragment(zero_event_journal(), corruption.first,
                                corruption.second));
    auto empty_writer = std::make_shared<MemoryWriter>();
    ov_msckf::CP2TraceJournalSink empty_sink(empty_writer);
    ASSERT_TRUE(empty_sink.Finalize());
    write_bytes(temporary.path + "/journal1", empty_writer->bytes);
    write_bytes(temporary.path + "/journal2", empty_writer->bytes);
    write_text(temporary.path + "/serial_pairs.jsonl", "");
    const std::string spec = temporary.path + "/spec.json";
    write_text(spec, make_spec(temporary.path, "V1_01_easy",
                               empty_digest.HexDigest()));
    EXPECT_EQ(run_assembler(
                  spec, temporary.path + "/output",
                  "cp2_recorded_assemble: journal decode failed: "
                  "canonical_codec_failure\n"),
              1)
        << "journal section tag " << corruption.first;
  }

  TemporaryDirectory rounding;
  ASSERT_FALSE(rounding.path.empty());
  ASSERT_EQ(::mkdir((rounding.path + "/output").c_str(), 0700), 0);
  write_bytes(rounding.path + "/journal0", one_gate_event_journal());
  auto empty_writer = std::make_shared<MemoryWriter>();
  ov_msckf::CP2TraceJournalSink empty_sink(empty_writer);
  ASSERT_TRUE(empty_sink.Finalize());
  write_bytes(rounding.path + "/journal1", empty_writer->bytes);
  write_bytes(rounding.path + "/journal2", empty_writer->bytes);
  const std::string serial =
      "{\"schema_version\":1,\"record_type\":\"serial_pair\","
      "\"sequence_index\":0,\"sequence_id\":\"MH_01_easy\","
      "\"pair_index\":0,\"anchor_filtered_index\":0,"
      "\"anchor_camera_id\":0,\"cam0_filtered_index\":0,"
      "\"cam1_filtered_index\":0,\"cam0_record_time_ns\":100,"
      "\"cam1_record_time_ns\":100,\"cam0_header_time_ns\":100,"
      "\"cam1_header_time_ns\":100,\"camera_timestamp_ns\":100,"
      "\"absolute_record_delta_ns\":0,\"selected\":true,"
      "\"enqueue_entered\":true,\"enqueue_returned\":true,"
      "\"enqueue_status\":\"queued\",\"processing_entered\":true,"
      "\"processing_returned\":true,\"processing_status\":\"processed\","
      "\"updater_invocation_ids\":[0]}\n";
  write_text(rounding.path + "/serial_pairs.jsonl", serial);
  ov_msckf::CP2Sha256 serial_digest;
  serial_digest.Update(serial);
  const std::string spec = rounding.path + "/spec.json";
  write_text(spec, make_spec(rounding.path, "V1_01_easy",
                             serial_digest.HexDigest()));
  ASSERT_EQ(std::fegetround(), FE_TONEAREST);
  EXPECT_EQ(run_assembler(spec, rounding.path + "/output",
                          "cp2_recorded_assemble: gate ratio requires "
                          "FE_TONEAREST\n",
                          FE_DOWNWARD),
            1);
  EXPECT_EQ(std::fegetround(), FE_TONEAREST);
  ASSERT_EQ(::mkdir((rounding.path + "/output_nearest").c_str(), 0700), 0);
  EXPECT_EQ(run_assembler(spec, rounding.path + "/output_nearest"), 0);

  TemporaryDirectory failed_statistics;
  ASSERT_FALSE(failed_statistics.path.empty());
  ASSERT_EQ(::mkdir((failed_statistics.path + "/output").c_str(), 0700), 0);
  write_bytes(failed_statistics.path + "/journal0",
              one_gate_event_journal(true));
  write_bytes(failed_statistics.path + "/journal1", empty_writer->bytes);
  write_bytes(failed_statistics.path + "/journal2", empty_writer->bytes);
  write_text(failed_statistics.path + "/serial_pairs.jsonl", serial);
  const std::string failed_spec = failed_statistics.path + "/spec.json";
  write_text(failed_spec,
             make_spec(failed_statistics.path, "V1_01_easy",
                       serial_digest.HexDigest()));
  ASSERT_EQ(run_assembler(failed_spec,
                          failed_statistics.path + "/output"), 0);
  const std::string failed_summary =
      read_text(failed_statistics.path + "/output/summary.json");
  const std::string failed_updates =
      read_text(failed_statistics.path + "/output/updates.jsonl");
  EXPECT_NE(failed_summary.find("\"per_feature_statistics_passed\":false"),
            std::string::npos);
  EXPECT_NE(failed_summary.find("\"math_passed\":false"),
            std::string::npos);
  EXPECT_NE(failed_updates.find("\"math_passed\":false"),
            std::string::npos);
}

} // namespace
