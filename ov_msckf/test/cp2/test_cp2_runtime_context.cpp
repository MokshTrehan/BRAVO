/*
 * SchurVIO-Lite CP2 strict runtime-context tests.
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include <gtest/gtest.h>

#include "update/CP2Canonical.h"
#include "update/CP2RuntimeContext.h"

#include <fcntl.h>
#include <string>
#include <sys/stat.h>
#include <unistd.h>

namespace {

using ov_msckf::CP2RuntimeContextExpectation;
using ov_msckf::CP2RuntimeContextStatus;

std::string quote(const std::string &value) { return "\"" + value + "\""; }

std::string nullable_path(bool available, const std::string &value) {
  return available ? quote(value) : "null";
}

std::string make_context(const std::string &level = "recorded_full",
                         const std::string &checkpoint = "CP2-C",
                         const std::string &mode = "nullspace",
                         bool shadow = true,
                         const std::string &root = "/tmp/cp2-partial/trace") {
  const bool recorded = level == "recorded_full";
  const bool sequence = level == "sequence";
  const bool timing = level == "timing";
  std::string result = "{";
  auto add = [&result](const std::string &key,
                       const std::string &value) {
    if (result.size() > 1U) {
      result += ",";
    }
    result += quote(key) + ":" + value;
  };
  add("schema_version", "1");
  add("record_type", quote("cp2_runtime_context"));
  add("checkpoint", quote(checkpoint));
  add("run_id", quote("synthetic-run"));
  add("sequence_index", "0");
  add("sequence_id", quote("MH_01_easy"));
  add("mode", quote(mode));
  add("shadow_enabled", shadow ? "true" : "false");
  add("trace_level", quote(level));
  add("source_commit", quote(std::string(40U, 'a')));
  add("config_sha256", quote(std::string(64U, 'b')));
  add("bag_sha256", quote(std::string(64U, 'c')));
  add("pair_index_sha256", "null");
  add("resolved_parameters_sha256", quote(std::string(64U, 'd')));
  add("trace_directory", quote(root));
  add("serial_trace_path", quote(root + "/serial.jsonl"));
  add("callback_trace_path", nullable_path(sequence || timing, root + "/callbacks.jsonl"));
  add("trajectory_trace_path", nullable_path(sequence, root + "/trajectory.jsonl"));
  add("updater_trace_path", nullable_path(recorded || timing, root + "/updates.bin"));
  add("state_payload_path", nullable_path(recorded, root + "/state.bin"));
  add("proposal_payload_path", nullable_path(recorded, root + "/proposal.bin"));
  add("raw_system_payload_path", nullable_path(recorded, root + "/raw.bin"));
  add("timing_trace_path", nullable_path(timing, root + "/timing.jsonl"));
  add("runtime_parameters_path", quote(root + "/parameters.bin"));
  add("loader_map_before_path", quote(root + "/loader-before.txt"));
  add("loader_map_after_path", quote(root + "/loader-after.txt"));
  add("legacy_state_path", nullable_path(sequence, root + "/state.txt"));
  add("legacy_deviation_path", nullable_path(sequence, root + "/deviation.txt"));
  add("legacy_timing_path", nullable_path(sequence, root + "/timing.csv"));
  result += "}";
  return result;
}

CP2RuntimeContextExpectation expectation(
    const std::string &level = "recorded_full",
    const std::string &checkpoint = "CP2-C",
    const std::string &mode = "nullspace", bool shadow = true,
    const std::string &root = "/tmp/cp2-partial/trace") {
  CP2RuntimeContextExpectation value;
  value.checkpoint = checkpoint;
  value.sequence_index = 0U;
  value.sequence_id = "MH_01_easy";
  value.mode = mode;
  value.shadow_enabled = shadow;
  value.trace_level = level;
  value.trace_directory = root;
  return value;
}

std::string replace_once(std::string value, const std::string &before,
                         const std::string &after) {
  const std::size_t position = value.find(before);
  EXPECT_NE(position, std::string::npos);
  if (position != std::string::npos) {
    value.replace(position, before.size(), after);
  }
  return value;
}

class TemporaryContextFile {
public:
  explicit TemporaryContextFile(const std::string &bytes) {
    char pattern[] = "/tmp/cp2-runtime-context-XXXXXX";
    char *created = ::mkdtemp(pattern);
    if (created == nullptr) {
      return;
    }
    directory_ = created;
    path_ = directory_ + "/context.json";
    const int descriptor = ::open(path_.c_str(), O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC,
                                  S_IRUSR | S_IWUSR);
    if (descriptor < 0) {
      return;
    }
    std::size_t offset = 0U;
    while (offset < bytes.size()) {
      const ssize_t count = ::write(descriptor, bytes.data() + offset,
                                    bytes.size() - offset);
      if (count <= 0) {
        break;
      }
      offset += static_cast<std::size_t>(count);
    }
    ::close(descriptor);
    complete_ = offset == bytes.size();
  }

  ~TemporaryContextFile() {
    if (!hardlink_.empty()) {
      ::unlink(hardlink_.c_str());
    }
    if (!symlink_.empty()) {
      ::unlink(symlink_.c_str());
    }
    if (!intermediate_symlink_.empty()) {
      ::unlink(intermediate_symlink_.c_str());
    }
    if (!path_.empty()) {
      ::unlink(path_.c_str());
    }
    if (!directory_.empty()) {
      ::rmdir(directory_.c_str());
    }
  }

  bool complete() const noexcept { return complete_; }
  const std::string &path() const noexcept { return path_; }

  std::string MakeHardlink() {
    hardlink_ = directory_ + "/hardlink.json";
    if (::link(path_.c_str(), hardlink_.c_str()) != 0) {
      hardlink_.clear();
    }
    return hardlink_;
  }

  std::string MakeSymlink() {
    symlink_ = directory_ + "/symlink.json";
    if (::symlink(path_.c_str(), symlink_.c_str()) != 0) {
      symlink_.clear();
    }
    return symlink_;
  }

  std::string MakeIntermediateSymlinkPath() {
    intermediate_symlink_ = directory_ + "-directory-link";
    if (::symlink(directory_.c_str(), intermediate_symlink_.c_str()) != 0) {
      intermediate_symlink_.clear();
      return std::string();
    }
    return intermediate_symlink_ + "/context.json";
  }

private:
  std::string directory_;
  std::string path_;
  std::string hardlink_;
  std::string symlink_;
  std::string intermediate_symlink_;
  bool complete_ = false;
};

TEST(CP2RuntimeContext, RecordedFullAcceptsExactDocumentAndHashesBytes) {
  const std::string document = make_context();
  const auto result = ov_msckf::ParseCP2RuntimeContext(document, expectation());
  ASSERT_TRUE(result.accepted())
      << ov_msckf::cp2_runtime_context_status_name(result.status);
  EXPECT_EQ(result.context.checkpoint, "CP2-C");
  EXPECT_EQ(result.context.sequence_index, 0U);
  EXPECT_EQ(result.context.sequence_id, "MH_01_easy");
  EXPECT_TRUE(result.context.shadow_enabled);
  EXPECT_TRUE(result.context.serial_trace_path.available);
  EXPECT_TRUE(result.context.updater_trace_path.available);
  EXPECT_FALSE(result.context.callback_trace_path.available);
  ov_msckf::CP2Sha256 digest;
  digest.Update(document);
  EXPECT_EQ(result.context.document_sha256, digest.HexDigest());
}

TEST(CP2RuntimeContext, SequenceAndTimingCombinationsAreExact) {
  const auto sequence = ov_msckf::ParseCP2RuntimeContext(
      make_context("sequence", "CP2-D", "schur", false),
      expectation("sequence", "CP2-D", "schur", false));
  ASSERT_TRUE(sequence.accepted());
  EXPECT_TRUE(sequence.context.callback_trace_path.available);
  EXPECT_TRUE(sequence.context.trajectory_trace_path.available);
  EXPECT_TRUE(sequence.context.legacy_state_path.available);
  EXPECT_FALSE(sequence.context.updater_trace_path.available);

  const auto timing = ov_msckf::ParseCP2RuntimeContext(
      make_context("timing", "CP2-E", "nullspace", false),
      expectation("timing", "CP2-E", "nullspace", false));
  ASSERT_TRUE(timing.accepted());
  EXPECT_TRUE(timing.context.callback_trace_path.available);
  EXPECT_TRUE(timing.context.updater_trace_path.available);
  EXPECT_TRUE(timing.context.timing_trace_path.available);
  EXPECT_FALSE(timing.context.trajectory_trace_path.available);
}

TEST(CP2RuntimeContext, DuplicateMissingAndExtraKeysReject) {
  const std::string valid = make_context();
  auto duplicate = replace_once(valid, "{", "{\"mode\":\"nullspace\",");
  EXPECT_EQ(ov_msckf::ParseCP2RuntimeContext(duplicate, expectation()).status,
            CP2RuntimeContextStatus::kDuplicateKey);
  auto missing = replace_once(valid, "\"pair_index_sha256\":null,", "");
  EXPECT_EQ(ov_msckf::ParseCP2RuntimeContext(missing, expectation()).status,
            CP2RuntimeContextStatus::kWrongKeyInventory);
  auto extra = replace_once(valid, "{", "{\"extra\":null,");
  EXPECT_EQ(ov_msckf::ParseCP2RuntimeContext(extra, expectation()).status,
            CP2RuntimeContextStatus::kWrongKeyInventory);
}

TEST(CP2RuntimeContext, JsonTypesNeverCoerce) {
  const std::string valid = make_context();
  EXPECT_EQ(ov_msckf::ParseCP2RuntimeContext(
                replace_once(valid, "\"schema_version\":1",
                             "\"schema_version\":\"1\""),
                expectation()).status,
            CP2RuntimeContextStatus::kWrongType);
  EXPECT_EQ(ov_msckf::ParseCP2RuntimeContext(
                replace_once(valid, "\"shadow_enabled\":true",
                             "\"shadow_enabled\":1"),
                expectation()).status,
            CP2RuntimeContextStatus::kWrongType);
  EXPECT_EQ(ov_msckf::ParseCP2RuntimeContext(
                replace_once(valid, "\"pair_index_sha256\":null",
                             "\"pair_index_sha256\":\"" +
                                 std::string(64U, 'e') + "\""),
                expectation()).status,
            CP2RuntimeContextStatus::kWrongType);
}

TEST(CP2RuntimeContext, SequenceAndLaunchExpectationsBindIdentity) {
  const std::string valid = make_context();
  auto wrong_sequence = replace_once(valid, "\"sequence_id\":\"MH_01_easy\"",
                                     "\"sequence_id\":\"MH_03_medium\"");
  EXPECT_EQ(ov_msckf::ParseCP2RuntimeContext(wrong_sequence, expectation()).status,
            CP2RuntimeContextStatus::kInvalidValue);
  auto expected = expectation();
  expected.mode = "schur";
  EXPECT_EQ(ov_msckf::ParseCP2RuntimeContext(valid, expected).status,
            CP2RuntimeContextStatus::kExpectationMismatch);
  expected = expectation();
  expected.trace_directory = "/tmp/a-different-root";
  EXPECT_EQ(ov_msckf::ParseCP2RuntimeContext(valid, expected).status,
            CP2RuntimeContextStatus::kExpectationMismatch);
}

TEST(CP2RuntimeContext, PathsRequireNormalizedDistinctStrictChildren) {
  const std::string valid = make_context();
  auto escape = replace_once(valid, "/tmp/cp2-partial/trace/updates.bin",
                             "/tmp/cp2-partial/trace/../updates.bin");
  EXPECT_EQ(ov_msckf::ParseCP2RuntimeContext(escape, expectation()).status,
            CP2RuntimeContextStatus::kInvalidPath);
  auto prefix = replace_once(valid, "/tmp/cp2-partial/trace/updates.bin",
                             "/tmp/cp2-partial/trace-sibling/updates.bin");
  EXPECT_EQ(ov_msckf::ParseCP2RuntimeContext(prefix, expectation()).status,
            CP2RuntimeContextStatus::kInvalidPath);
  auto duplicate = replace_once(valid, "/tmp/cp2-partial/trace/proposal.bin",
                                "/tmp/cp2-partial/trace/state.bin");
  EXPECT_EQ(ov_msckf::ParseCP2RuntimeContext(duplicate, expectation()).status,
            CP2RuntimeContextStatus::kInvalidPath);
}

TEST(CP2RuntimeContext, OutputPresenceCannotCrossTraceLevels) {
  const std::string valid = make_context();
  auto callback = replace_once(valid, "\"callback_trace_path\":null",
                               "\"callback_trace_path\":\"/tmp/cp2-partial/trace/callback.jsonl\"");
  EXPECT_EQ(ov_msckf::ParseCP2RuntimeContext(callback, expectation()).status,
            CP2RuntimeContextStatus::kInvalidOutputCombination);
  auto missing_raw = replace_once(valid,
                                  "\"raw_system_payload_path\":\"/tmp/cp2-partial/trace/raw.bin\"",
                                  "\"raw_system_payload_path\":null");
  EXPECT_EQ(ov_msckf::ParseCP2RuntimeContext(missing_raw, expectation()).status,
            CP2RuntimeContextStatus::kInvalidOutputCombination);
}

TEST(CP2RuntimeContext, EscapesAreStrictAndDecodedBeforeValidation) {
  const std::string valid = make_context();
  auto escaped = replace_once(valid, "synthetic-run", "synthetic\\u002drun");
  const auto accepted = ov_msckf::ParseCP2RuntimeContext(escaped, expectation());
  ASSERT_TRUE(accepted.accepted());
  EXPECT_EQ(accepted.context.run_id, "synthetic-run");
  auto nul = replace_once(valid, "synthetic-run", "synthetic\\u0000run");
  EXPECT_EQ(ov_msckf::ParseCP2RuntimeContext(nul, expectation()).status,
            CP2RuntimeContextStatus::kInvalidValue);
  auto surrogate = replace_once(valid, "synthetic-run", "synthetic\\ud800run");
  EXPECT_EQ(ov_msckf::ParseCP2RuntimeContext(surrogate, expectation()).status,
            CP2RuntimeContextStatus::kInvalidJson);
}

TEST(CP2RuntimeContext, NonJsonNumbersConstantsAndTrailingBytesReject) {
  const std::string valid = make_context();
  auto floating = replace_once(valid, "\"sequence_index\":0",
                               "\"sequence_index\":0.0");
  EXPECT_EQ(ov_msckf::ParseCP2RuntimeContext(floating, expectation()).status,
            CP2RuntimeContextStatus::kInvalidJson);
  auto negative = replace_once(valid, "\"sequence_index\":0",
                               "\"sequence_index\":-1");
  EXPECT_EQ(ov_msckf::ParseCP2RuntimeContext(negative, expectation()).status,
            CP2RuntimeContextStatus::kInvalidJson);
  EXPECT_EQ(ov_msckf::ParseCP2RuntimeContext(valid + "x", expectation()).status,
            CP2RuntimeContextStatus::kInvalidJson);
}

TEST(CP2RuntimeContext, RejectionIsFailureAtomicAndStatusNamesAreStable) {
  const auto rejected = ov_msckf::ParseCP2RuntimeContext("{}", expectation());
  EXPECT_FALSE(rejected.accepted());
  EXPECT_TRUE(rejected.context.run_id.empty());
  EXPECT_TRUE(rejected.context.document_sha256.empty());
  EXPECT_STREQ(ov_msckf::cp2_runtime_context_status_name(rejected.status),
               "wrong_key_inventory");
  EXPECT_STREQ(ov_msckf::cp2_runtime_trace_level_name(
                   ov_msckf::CP2RuntimeTraceLevel::kTiming),
               "timing");
}

TEST(CP2RuntimeContext, DescriptorBoundFileReadAcceptsExactBytesAndBound) {
  const std::string document = make_context();
  TemporaryContextFile file(document);
  ASSERT_TRUE(file.complete());
  const auto accepted = ov_msckf::ReadCP2RuntimeContextFile(
      file.path(), expectation(), static_cast<std::uint64_t>(document.size()));
  ASSERT_TRUE(accepted.accepted());
  EXPECT_EQ(accepted.context.document_sha256,
            ov_msckf::ParseCP2RuntimeContext(document, expectation())
                .context.document_sha256);
  EXPECT_EQ(ov_msckf::ReadCP2RuntimeContextFile(
                file.path(), expectation(),
                static_cast<std::uint64_t>(document.size() - 1U))
                .status,
            CP2RuntimeContextStatus::kFileTooLarge);
}

TEST(CP2RuntimeContext, DescriptorBoundFileReadRejectsRelativeSymlinkAndHardlink) {
  TemporaryContextFile file(make_context());
  ASSERT_TRUE(file.complete());
  EXPECT_EQ(ov_msckf::ReadCP2RuntimeContextFile("relative.json", expectation()).status,
            CP2RuntimeContextStatus::kInvalidPath);
  const std::string symlink = file.MakeSymlink();
  ASSERT_FALSE(symlink.empty());
  EXPECT_EQ(ov_msckf::ReadCP2RuntimeContextFile(symlink, expectation()).status,
            CP2RuntimeContextStatus::kFileIdentityFailure);
  const std::string hardlink = file.MakeHardlink();
  ASSERT_FALSE(hardlink.empty());
  EXPECT_EQ(ov_msckf::ReadCP2RuntimeContextFile(hardlink, expectation()).status,
            CP2RuntimeContextStatus::kFileIdentityFailure);
  EXPECT_EQ(ov_msckf::ReadCP2RuntimeContextFile(file.path(), expectation()).status,
            CP2RuntimeContextStatus::kFileIdentityFailure);
  const std::string intermediate = file.MakeIntermediateSymlinkPath();
  ASSERT_FALSE(intermediate.empty());
  EXPECT_EQ(ov_msckf::ReadCP2RuntimeContextFile(intermediate, expectation()).status,
            CP2RuntimeContextStatus::kFileIdentityFailure);
}

} // namespace
