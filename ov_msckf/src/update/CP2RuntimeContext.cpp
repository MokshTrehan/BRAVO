/*
 * SchurVIO-Lite CP2 strict runtime-context parser.
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include "CP2RuntimeContext.h"

#include "CP2Canonical.h"

#include <array>
#include <cerrno>
#include <cstddef>
#include <cstdint>
#include <fcntl.h>
#include <limits>
#include <map>
#include <set>
#include <string>
#include <sys/stat.h>
#include <unistd.h>
#include <utility>
#include <vector>

namespace {

enum class JsonKind : std::uint8_t { kNull, kBool, kU64, kString };

struct JsonValue {
  JsonKind kind = JsonKind::kNull;
  bool boolean = false;
  std::uint64_t integer = 0U;
  std::string string;
};

bool is_continuation(unsigned char value) noexcept {
  return (value & UINT8_C(0xc0)) == UINT8_C(0x80);
}

bool valid_utf8(const std::string &value) noexcept {
  std::size_t index = 0U;
  while (index < value.size()) {
    const unsigned char first = static_cast<unsigned char>(value[index]);
    if (first <= UINT8_C(0x7f)) {
      ++index;
      continue;
    }
    if (first >= UINT8_C(0xc2) && first <= UINT8_C(0xdf)) {
      if (index + 1U >= value.size() ||
          !is_continuation(static_cast<unsigned char>(value[index + 1U]))) {
        return false;
      }
      index += 2U;
      continue;
    }
    if (first >= UINT8_C(0xe0) && first <= UINT8_C(0xef)) {
      if (index + 2U >= value.size()) {
        return false;
      }
      const unsigned char second = static_cast<unsigned char>(value[index + 1U]);
      const unsigned char third = static_cast<unsigned char>(value[index + 2U]);
      if (!is_continuation(second) || !is_continuation(third) ||
          (first == UINT8_C(0xe0) && second < UINT8_C(0xa0)) ||
          (first == UINT8_C(0xed) && second >= UINT8_C(0xa0))) {
        return false;
      }
      index += 3U;
      continue;
    }
    if (first >= UINT8_C(0xf0) && first <= UINT8_C(0xf4)) {
      if (index + 3U >= value.size()) {
        return false;
      }
      const unsigned char second = static_cast<unsigned char>(value[index + 1U]);
      const unsigned char third = static_cast<unsigned char>(value[index + 2U]);
      const unsigned char fourth = static_cast<unsigned char>(value[index + 3U]);
      if (!is_continuation(second) || !is_continuation(third) ||
          !is_continuation(fourth) ||
          (first == UINT8_C(0xf0) && second < UINT8_C(0x90)) ||
          (first == UINT8_C(0xf4) && second >= UINT8_C(0x90))) {
        return false;
      }
      index += 4U;
      continue;
    }
    return false;
  }
  return true;
}

void append_utf8_codepoint(std::uint32_t value, std::string &output) {
  if (value <= UINT32_C(0x7f)) {
    output.push_back(static_cast<char>(value));
  } else if (value <= UINT32_C(0x7ff)) {
    output.push_back(static_cast<char>(UINT32_C(0xc0) | (value >> 6U)));
    output.push_back(static_cast<char>(UINT32_C(0x80) | (value & UINT32_C(0x3f))));
  } else if (value <= UINT32_C(0xffff)) {
    output.push_back(static_cast<char>(UINT32_C(0xe0) | (value >> 12U)));
    output.push_back(static_cast<char>(UINT32_C(0x80) | ((value >> 6U) & UINT32_C(0x3f))));
    output.push_back(static_cast<char>(UINT32_C(0x80) | (value & UINT32_C(0x3f))));
  } else {
    output.push_back(static_cast<char>(UINT32_C(0xf0) | (value >> 18U)));
    output.push_back(static_cast<char>(UINT32_C(0x80) | ((value >> 12U) & UINT32_C(0x3f))));
    output.push_back(static_cast<char>(UINT32_C(0x80) | ((value >> 6U) & UINT32_C(0x3f))));
    output.push_back(static_cast<char>(UINT32_C(0x80) | (value & UINT32_C(0x3f))));
  }
}

class StrictObjectParser {
public:
  explicit StrictObjectParser(const std::string &input) : input_(input) {}

  bool Parse(std::map<std::string, JsonValue> &output,
             ov_msckf::CP2RuntimeContextStatus &status) {
    SkipWhitespace();
    if (!Take('{')) {
      status = ov_msckf::CP2RuntimeContextStatus::kInvalidJson;
      return false;
    }
    SkipWhitespace();
    if (Take('}')) {
      SkipWhitespace();
      return position_ == input_.size();
    }
    while (position_ < input_.size()) {
      std::string key;
      if (!ParseString(key)) {
        status = ov_msckf::CP2RuntimeContextStatus::kInvalidJson;
        return false;
      }
      if (output.find(key) != output.end()) {
        status = ov_msckf::CP2RuntimeContextStatus::kDuplicateKey;
        return false;
      }
      SkipWhitespace();
      if (!Take(':')) {
        status = ov_msckf::CP2RuntimeContextStatus::kInvalidJson;
        return false;
      }
      SkipWhitespace();
      JsonValue value;
      if (!ParseValue(value)) {
        status = ov_msckf::CP2RuntimeContextStatus::kInvalidJson;
        return false;
      }
      output.emplace(std::move(key), std::move(value));
      SkipWhitespace();
      if (Take('}')) {
        SkipWhitespace();
        if (position_ != input_.size()) {
          status = ov_msckf::CP2RuntimeContextStatus::kInvalidJson;
          return false;
        }
        return true;
      }
      if (!Take(',')) {
        status = ov_msckf::CP2RuntimeContextStatus::kInvalidJson;
        return false;
      }
      SkipWhitespace();
    }
    status = ov_msckf::CP2RuntimeContextStatus::kInvalidJson;
    return false;
  }

private:
  void SkipWhitespace() noexcept {
    while (position_ < input_.size() &&
           (input_[position_] == ' ' || input_[position_] == '\t' ||
            input_[position_] == '\r' || input_[position_] == '\n')) {
      ++position_;
    }
  }

  bool Take(char expected) noexcept {
    if (position_ >= input_.size() || input_[position_] != expected) {
      return false;
    }
    ++position_;
    return true;
  }

  bool ParseHex4(std::uint32_t &value) noexcept {
    if (position_ > input_.size() || input_.size() - position_ < 4U) {
      return false;
    }
    value = 0U;
    for (std::size_t offset = 0U; offset < 4U; ++offset) {
      const char character = input_[position_ + offset];
      std::uint32_t digit = 0U;
      if (character >= '0' && character <= '9') {
        digit = static_cast<std::uint32_t>(character - '0');
      } else if (character >= 'a' && character <= 'f') {
        digit = static_cast<std::uint32_t>(character - 'a' + 10);
      } else if (character >= 'A' && character <= 'F') {
        digit = static_cast<std::uint32_t>(character - 'A' + 10);
      } else {
        return false;
      }
      value = (value << 4U) | digit;
    }
    position_ += 4U;
    return true;
  }

  bool ParseString(std::string &output) {
    if (!Take('"')) {
      return false;
    }
    while (position_ < input_.size()) {
      const unsigned char byte = static_cast<unsigned char>(input_[position_++]);
      if (byte == static_cast<unsigned char>('"')) {
        return valid_utf8(output);
      }
      if (byte < UINT8_C(0x20)) {
        return false;
      }
      if (byte != static_cast<unsigned char>('\\')) {
        output.push_back(static_cast<char>(byte));
        continue;
      }
      if (position_ >= input_.size()) {
        return false;
      }
      const char escaped = input_[position_++];
      switch (escaped) {
      case '"': output.push_back('"'); break;
      case '\\': output.push_back('\\'); break;
      case '/': output.push_back('/'); break;
      case 'b': output.push_back('\b'); break;
      case 'f': output.push_back('\f'); break;
      case 'n': output.push_back('\n'); break;
      case 'r': output.push_back('\r'); break;
      case 't': output.push_back('\t'); break;
      case 'u': {
        std::uint32_t first = 0U;
        if (!ParseHex4(first)) {
          return false;
        }
        std::uint32_t codepoint = first;
        if (first >= UINT32_C(0xd800) && first <= UINT32_C(0xdbff)) {
          if (position_ + 2U > input_.size() || input_[position_] != '\\' ||
              input_[position_ + 1U] != 'u') {
            return false;
          }
          position_ += 2U;
          std::uint32_t second = 0U;
          if (!ParseHex4(second) || second < UINT32_C(0xdc00) ||
              second > UINT32_C(0xdfff)) {
            return false;
          }
          codepoint = UINT32_C(0x10000) +
                      ((first - UINT32_C(0xd800)) << 10U) +
                      (second - UINT32_C(0xdc00));
        } else if (first >= UINT32_C(0xdc00) && first <= UINT32_C(0xdfff)) {
          return false;
        }
        append_utf8_codepoint(codepoint, output);
        break;
      }
      default:
        return false;
      }
    }
    return false;
  }

  bool ParseValue(JsonValue &output) {
    if (position_ >= input_.size()) {
      return false;
    }
    if (input_[position_] == '"') {
      output.kind = JsonKind::kString;
      return ParseString(output.string);
    }
    if (input_.compare(position_, 4U, "null") == 0) {
      position_ += 4U;
      output.kind = JsonKind::kNull;
      return true;
    }
    if (input_.compare(position_, 4U, "true") == 0) {
      position_ += 4U;
      output.kind = JsonKind::kBool;
      output.boolean = true;
      return true;
    }
    if (input_.compare(position_, 5U, "false") == 0) {
      position_ += 5U;
      output.kind = JsonKind::kBool;
      output.boolean = false;
      return true;
    }
    if (input_[position_] < '0' || input_[position_] > '9') {
      return false;
    }
    const std::size_t beginning = position_;
    if (input_[position_] == '0') {
      ++position_;
      if (position_ < input_.size() && input_[position_] >= '0' &&
          input_[position_] <= '9') {
        return false;
      }
    } else {
      while (position_ < input_.size() && input_[position_] >= '0' &&
             input_[position_] <= '9') {
        ++position_;
      }
    }
    std::uint64_t value = 0U;
    for (std::size_t index = beginning; index < position_; ++index) {
      const std::uint64_t digit =
          static_cast<std::uint64_t>(input_[index] - '0');
      if (value > (std::numeric_limits<std::uint64_t>::max() - digit) / 10U) {
        return false;
      }
      value = value * 10U + digit;
    }
    output.kind = JsonKind::kU64;
    output.integer = value;
    return true;
  }

  const std::string &input_;
  std::size_t position_ = 0U;
};

const std::array<const char *, 29U> kExactKeys = {{
    "schema_version", "record_type", "checkpoint", "run_id",
    "sequence_index", "sequence_id", "mode", "shadow_enabled",
    "trace_level", "source_commit", "config_sha256", "bag_sha256",
    "pair_index_sha256", "resolved_parameters_sha256", "trace_directory",
    "serial_trace_path", "callback_trace_path", "trajectory_trace_path",
    "updater_trace_path", "state_payload_path", "proposal_payload_path",
    "raw_system_payload_path", "timing_trace_path",
    "runtime_parameters_path", "loader_map_before_path",
    "loader_map_after_path", "legacy_state_path", "legacy_deviation_path",
    "legacy_timing_path",
}};

bool exact_key_inventory(const std::map<std::string, JsonValue> &values) {
  if (values.size() != kExactKeys.size()) {
    return false;
  }
  for (const char *key : kExactKeys) {
    if (values.find(key) == values.end()) {
      return false;
    }
  }
  return true;
}

const JsonValue &field(const std::map<std::string, JsonValue> &values,
                       const char *key) {
  return values.find(key)->second;
}

bool string_field(const std::map<std::string, JsonValue> &values,
                  const char *key, std::string &output) {
  const JsonValue &value = field(values, key);
  if (value.kind != JsonKind::kString) {
    return false;
  }
  output = value.string;
  return true;
}

bool nullable_path_field(const std::map<std::string, JsonValue> &values,
                         const char *key, ov_msckf::CP2NullablePath &output) {
  const JsonValue &value = field(values, key);
  if (value.kind == JsonKind::kNull) {
    output = ov_msckf::CP2NullablePath{};
    return true;
  }
  if (value.kind != JsonKind::kString) {
    return false;
  }
  output.available = true;
  output.value = value.string;
  return true;
}

bool lowercase_hex(const std::string &value, std::size_t size) noexcept {
  if (value.size() != size) {
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

bool safe_id(const std::string &value) noexcept {
  if (value.empty() || value.size() > 128U) {
    return false;
  }
  for (std::size_t index = 0U; index < value.size(); ++index) {
    const char character = value[index];
    const bool alphanumeric =
        (character >= 'a' && character <= 'z') ||
        (character >= 'A' && character <= 'Z') ||
        (character >= '0' && character <= '9');
    if (!alphanumeric && (index == 0U ||
                          (character != '.' && character != '_' &&
                           character != '-'))) {
      return false;
    }
  }
  return true;
}

bool normalized_absolute_path(const std::string &value) noexcept {
  if (value.size() < 2U || value.front() != '/' || value.back() == '/') {
    return false;
  }
  std::size_t beginning = 1U;
  while (beginning < value.size()) {
    const std::size_t slash = value.find('/', beginning);
    const std::size_t ending =
        slash == std::string::npos ? value.size() : slash;
    if (ending == beginning) {
      return false;
    }
    const std::string component = value.substr(beginning, ending - beginning);
    if (component == "." || component == ".." ||
        component.find('\0') != std::string::npos) {
      return false;
    }
    if (slash == std::string::npos) {
      return true;
    }
    beginning = slash + 1U;
  }
  return false;
}

bool strict_child(const std::string &path, const std::string &root) noexcept {
  return path.size() > root.size() + 1U &&
         path.compare(0U, root.size(), root) == 0 &&
         path[root.size()] == '/';
}

bool same_stat_identity(const struct stat &left,
                        const struct stat &right) noexcept {
  return left.st_dev == right.st_dev && left.st_ino == right.st_ino &&
         left.st_mode == right.st_mode && left.st_nlink == right.st_nlink &&
         left.st_uid == right.st_uid && left.st_gid == right.st_gid &&
         left.st_size == right.st_size &&
         left.st_mtim.tv_sec == right.st_mtim.tv_sec &&
         left.st_mtim.tv_nsec == right.st_mtim.tv_nsec &&
         left.st_ctim.tv_sec == right.st_ctim.tv_sec &&
         left.st_ctim.tv_nsec == right.st_ctim.tv_nsec;
}

bool expected_sequence(std::uint64_t index, const std::string &id) noexcept {
  return (index == 0U && id == "MH_01_easy") ||
         (index == 1U && id == "MH_03_medium") ||
         (index == 2U && id == "V1_01_easy");
}

bool same_expectation(const ov_msckf::CP2RuntimeContext &context,
                      const ov_msckf::CP2RuntimeContextExpectation &expected) {
  return context.checkpoint == expected.checkpoint &&
         context.sequence_index == expected.sequence_index &&
         context.sequence_id == expected.sequence_id &&
         context.mode == expected.mode &&
         context.shadow_enabled == expected.shadow_enabled &&
         std::string(ov_msckf::cp2_runtime_trace_level_name(context.trace_level)) ==
             expected.trace_level &&
         context.trace_directory == expected.trace_directory;
}

bool validate_output_paths(const ov_msckf::CP2RuntimeContext &context) {
  const std::array<const ov_msckf::CP2NullablePath *, 14U> paths = {{
      &context.serial_trace_path, &context.callback_trace_path,
      &context.trajectory_trace_path, &context.updater_trace_path,
      &context.state_payload_path, &context.proposal_payload_path,
      &context.raw_system_payload_path, &context.timing_trace_path,
      &context.runtime_parameters_path, &context.loader_map_before_path,
      &context.loader_map_after_path, &context.legacy_state_path,
      &context.legacy_deviation_path, &context.legacy_timing_path,
  }};
  if (!normalized_absolute_path(context.trace_directory)) {
    return false;
  }
  std::set<std::string> unique;
  for (const ov_msckf::CP2NullablePath *path : paths) {
    if (!path->available) {
      if (!path->value.empty()) {
        return false;
      }
      continue;
    }
    if (!normalized_absolute_path(path->value) ||
        !strict_child(path->value, context.trace_directory) ||
        !unique.insert(path->value).second) {
      return false;
    }
  }
  return true;
}

bool validate_output_combination(const ov_msckf::CP2RuntimeContext &context) {
  const bool common = context.serial_trace_path.available &&
                      context.runtime_parameters_path.available &&
                      context.loader_map_before_path.available &&
                      context.loader_map_after_path.available;
  if (!common) {
    return false;
  }
  if (context.trace_level == ov_msckf::CP2RuntimeTraceLevel::kRecordedFull) {
    return context.checkpoint == "CP2-C" && context.mode == "nullspace" &&
           context.shadow_enabled && context.updater_trace_path.available &&
           context.state_payload_path.available &&
           context.proposal_payload_path.available &&
           context.raw_system_payload_path.available &&
           !context.callback_trace_path.available &&
           !context.trajectory_trace_path.available &&
           !context.timing_trace_path.available &&
           !context.legacy_state_path.available &&
           !context.legacy_deviation_path.available &&
           !context.legacy_timing_path.available;
  }
  if (context.trace_level == ov_msckf::CP2RuntimeTraceLevel::kSequence) {
    return context.checkpoint == "CP2-D" &&
           (context.mode == "nullspace" || context.mode == "schur") &&
           !context.shadow_enabled && context.callback_trace_path.available &&
           context.trajectory_trace_path.available &&
           context.legacy_state_path.available &&
           context.legacy_deviation_path.available &&
           context.legacy_timing_path.available &&
           !context.updater_trace_path.available &&
           !context.state_payload_path.available &&
           !context.proposal_payload_path.available &&
           !context.raw_system_payload_path.available &&
           !context.timing_trace_path.available;
  }
  return context.checkpoint == "CP2-E" &&
         (context.mode == "nullspace" || context.mode == "schur") &&
         !context.shadow_enabled && context.callback_trace_path.available &&
         context.updater_trace_path.available &&
         context.timing_trace_path.available &&
         !context.trajectory_trace_path.available &&
         !context.state_payload_path.available &&
         !context.proposal_payload_path.available &&
         !context.raw_system_payload_path.available &&
         !context.legacy_state_path.available &&
         !context.legacy_deviation_path.available &&
         !context.legacy_timing_path.available;
}

} // namespace

const char *ov_msckf::cp2_runtime_trace_level_name(
    CP2RuntimeTraceLevel level) noexcept {
  switch (level) {
  case CP2RuntimeTraceLevel::kRecordedFull: return "recorded_full";
  case CP2RuntimeTraceLevel::kSequence: return "sequence";
  case CP2RuntimeTraceLevel::kTiming: return "timing";
  }
  return "invalid";
}

const char *ov_msckf::cp2_runtime_context_status_name(
    CP2RuntimeContextStatus status) noexcept {
  switch (status) {
  case CP2RuntimeContextStatus::kAccepted: return "accepted";
  case CP2RuntimeContextStatus::kInvalidJson: return "invalid_json";
  case CP2RuntimeContextStatus::kDuplicateKey: return "duplicate_key";
  case CP2RuntimeContextStatus::kWrongKeyInventory: return "wrong_key_inventory";
  case CP2RuntimeContextStatus::kWrongType: return "wrong_type";
  case CP2RuntimeContextStatus::kInvalidValue: return "invalid_value";
  case CP2RuntimeContextStatus::kExpectationMismatch: return "expectation_mismatch";
  case CP2RuntimeContextStatus::kInvalidPath: return "invalid_path";
  case CP2RuntimeContextStatus::kInvalidOutputCombination: return "invalid_output_combination";
  case CP2RuntimeContextStatus::kFileOpenFailure: return "file_open_failure";
  case CP2RuntimeContextStatus::kFileIdentityFailure: return "file_identity_failure";
  case CP2RuntimeContextStatus::kFileTooLarge: return "file_too_large";
  case CP2RuntimeContextStatus::kFileReadFailure: return "file_read_failure";
  case CP2RuntimeContextStatus::kAllocationFailure: return "allocation_failure";
  }
  return "invalid_status";
}

ov_msckf::CP2RuntimeContextResult ov_msckf::ReadCP2RuntimeContextFile(
    const std::string &path, const CP2RuntimeContextExpectation &expectation,
    std::uint64_t maximum_bytes) noexcept {
  CP2RuntimeContextResult failure;
  if (!normalized_absolute_path(path)) {
    failure.status = CP2RuntimeContextStatus::kInvalidPath;
    return failure;
  }
  struct BoundDirectory {
    int descriptor = -1;
    std::string name;
    struct stat identity {};
  };
  std::vector<BoundDirectory> directories;
  int descriptor = -1;
  const auto close_all = [&directories, &descriptor]() noexcept {
    if (descriptor >= 0) {
      ::close(descriptor);
      descriptor = -1;
    }
    for (auto iterator = directories.rbegin();
         iterator != directories.rend(); ++iterator) {
      if (iterator->descriptor >= 0) {
        ::close(iterator->descriptor);
        iterator->descriptor = -1;
      }
    }
  };
  const auto fail = [&close_all](CP2RuntimeContextStatus status) {
    close_all();
    CP2RuntimeContextResult value;
    value.status = status;
    return value;
  };
  try {
    std::vector<std::string> components;
    std::size_t beginning = 1U;
    while (beginning < path.size()) {
      const std::size_t slash = path.find('/', beginning);
      const std::size_t ending =
          slash == std::string::npos ? path.size() : slash;
      components.push_back(path.substr(beginning, ending - beginning));
      if (slash == std::string::npos) {
        break;
      }
      beginning = slash + 1U;
    }
    if (components.empty()) {
      return fail(CP2RuntimeContextStatus::kInvalidPath);
    }
    directories.reserve(components.size());
    BoundDirectory root;
    root.descriptor = ::open("/", O_RDONLY | O_DIRECTORY | O_CLOEXEC |
                                      O_NOFOLLOW);
    if (root.descriptor < 0 || ::fstat(root.descriptor, &root.identity) != 0 ||
        !S_ISDIR(root.identity.st_mode)) {
      if (root.descriptor >= 0) {
        ::close(root.descriptor);
      }
      return fail(CP2RuntimeContextStatus::kFileOpenFailure);
    }
    directories.push_back(std::move(root));
    for (std::size_t index = 0U; index + 1U < components.size(); ++index) {
      BoundDirectory child;
      child.name = components[index];
      struct stat before {};
      if (::fstatat(directories.back().descriptor, child.name.c_str(),
                    &before, AT_SYMLINK_NOFOLLOW) != 0) {
        return fail(CP2RuntimeContextStatus::kFileOpenFailure);
      }
      if (!S_ISDIR(before.st_mode)) {
        return fail(CP2RuntimeContextStatus::kFileIdentityFailure);
      }
      child.descriptor =
          ::openat(directories.back().descriptor, child.name.c_str(),
                   O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW);
      if (child.descriptor < 0) {
        return fail(CP2RuntimeContextStatus::kFileOpenFailure);
      }
      struct stat opened {};
      if (::fstat(child.descriptor, &opened) != 0 ||
          !same_stat_identity(before, opened)) {
        ::close(child.descriptor);
        child.descriptor = -1;
        return fail(CP2RuntimeContextStatus::kFileIdentityFailure);
      }
      child.identity = before;
      directories.push_back(std::move(child));
    }
    const std::string &leaf = components.back();
    struct stat path_before {};
    if (::fstatat(directories.back().descriptor, leaf.c_str(), &path_before,
                  AT_SYMLINK_NOFOLLOW) != 0) {
      return fail(CP2RuntimeContextStatus::kFileOpenFailure);
    }
    if (!S_ISREG(path_before.st_mode) || path_before.st_nlink != 1U) {
      return fail(CP2RuntimeContextStatus::kFileIdentityFailure);
    }
    descriptor = ::openat(directories.back().descriptor, leaf.c_str(),
                          O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
    if (descriptor < 0) {
      return fail(CP2RuntimeContextStatus::kFileOpenFailure);
    }
    struct stat opened {};
    if (::fstat(descriptor, &opened) != 0 ||
        !same_stat_identity(path_before, opened)) {
      return fail(CP2RuntimeContextStatus::kFileIdentityFailure);
    }
    if (opened.st_size < 0 ||
        static_cast<std::uint64_t>(opened.st_size) > maximum_bytes ||
        static_cast<std::uint64_t>(opened.st_size) >
            static_cast<std::uint64_t>(
                std::numeric_limits<std::size_t>::max())) {
      return fail(CP2RuntimeContextStatus::kFileTooLarge);
    }
    std::string document(static_cast<std::size_t>(opened.st_size), '\0');
    std::size_t offset = 0U;
    while (offset < document.size()) {
      const ssize_t count = ::read(descriptor, &document[offset],
                                   document.size() - offset);
      if (count < 0 && errno == EINTR) {
        continue;
      }
      if (count <= 0) {
        return fail(CP2RuntimeContextStatus::kFileReadFailure);
      }
      offset += static_cast<std::size_t>(count);
    }
    char trailing = '\0';
    ssize_t trailing_count = -1;
    do {
      trailing_count = ::read(descriptor, &trailing, 1U);
    } while (trailing_count < 0 && errno == EINTR);
    struct stat opened_after {};
    struct stat path_after {};
    if (trailing_count != 0 || ::fstat(descriptor, &opened_after) != 0 ||
        ::fstatat(directories.back().descriptor, leaf.c_str(), &path_after,
                  AT_SYMLINK_NOFOLLOW) != 0) {
      return fail(CP2RuntimeContextStatus::kFileReadFailure);
    }
    if (!same_stat_identity(path_before, opened_after) ||
        !same_stat_identity(path_before, path_after)) {
      return fail(CP2RuntimeContextStatus::kFileIdentityFailure);
    }
    for (std::size_t index = 0U; index < directories.size(); ++index) {
      struct stat held {};
      if (::fstat(directories[index].descriptor, &held) != 0 ||
          !same_stat_identity(directories[index].identity, held)) {
        return fail(CP2RuntimeContextStatus::kFileIdentityFailure);
      }
      if (index > 0U) {
        struct stat rebound {};
        if (::fstatat(directories[index - 1U].descriptor,
                      directories[index].name.c_str(), &rebound,
                      AT_SYMLINK_NOFOLLOW) != 0 ||
            !same_stat_identity(directories[index].identity, rebound)) {
          return fail(CP2RuntimeContextStatus::kFileIdentityFailure);
        }
      }
    }
    CP2RuntimeContextResult parsed =
        ParseCP2RuntimeContext(document, expectation);
    if (::close(descriptor) != 0) {
      descriptor = -1;
      close_all();
      CP2RuntimeContextResult close_failure;
      close_failure.status = CP2RuntimeContextStatus::kFileReadFailure;
      return close_failure;
    }
    descriptor = -1;
    close_all();
    return parsed;
  } catch (...) {
    return fail(CP2RuntimeContextStatus::kAllocationFailure);
  }
}

ov_msckf::CP2RuntimeContextResult ov_msckf::ParseCP2RuntimeContext(
    const std::string &document,
    const CP2RuntimeContextExpectation &expectation) noexcept {
  CP2RuntimeContextResult result;
  try {
    std::map<std::string, JsonValue> values;
    StrictObjectParser parser(document);
    if (!parser.Parse(values, result.status)) {
      return result;
    }
    if (!exact_key_inventory(values)) {
      result.status = CP2RuntimeContextStatus::kWrongKeyInventory;
      return result;
    }
    const JsonValue &schema_version = field(values, "schema_version");
    const JsonValue &sequence_index = field(values, "sequence_index");
    const JsonValue &shadow_enabled = field(values, "shadow_enabled");
    const JsonValue &pair_index_sha256 = field(values, "pair_index_sha256");
    if (schema_version.kind != JsonKind::kU64 ||
        sequence_index.kind != JsonKind::kU64 ||
        shadow_enabled.kind != JsonKind::kBool ||
        pair_index_sha256.kind != JsonKind::kNull) {
      result.status = CP2RuntimeContextStatus::kWrongType;
      return result;
    }

    CP2RuntimeContext context;
    std::string record_type;
    std::string trace_level;
    if (!string_field(values, "record_type", record_type) ||
        !string_field(values, "checkpoint", context.checkpoint) ||
        !string_field(values, "run_id", context.run_id) ||
        !string_field(values, "sequence_id", context.sequence_id) ||
        !string_field(values, "mode", context.mode) ||
        !string_field(values, "trace_level", trace_level) ||
        !string_field(values, "source_commit", context.source_commit) ||
        !string_field(values, "config_sha256", context.config_sha256) ||
        !string_field(values, "bag_sha256", context.bag_sha256) ||
        !string_field(values, "resolved_parameters_sha256",
                      context.resolved_parameters_sha256) ||
        !string_field(values, "trace_directory", context.trace_directory) ||
        !nullable_path_field(values, "serial_trace_path", context.serial_trace_path) ||
        !nullable_path_field(values, "callback_trace_path", context.callback_trace_path) ||
        !nullable_path_field(values, "trajectory_trace_path", context.trajectory_trace_path) ||
        !nullable_path_field(values, "updater_trace_path", context.updater_trace_path) ||
        !nullable_path_field(values, "state_payload_path", context.state_payload_path) ||
        !nullable_path_field(values, "proposal_payload_path", context.proposal_payload_path) ||
        !nullable_path_field(values, "raw_system_payload_path", context.raw_system_payload_path) ||
        !nullable_path_field(values, "timing_trace_path", context.timing_trace_path) ||
        !nullable_path_field(values, "runtime_parameters_path", context.runtime_parameters_path) ||
        !nullable_path_field(values, "loader_map_before_path", context.loader_map_before_path) ||
        !nullable_path_field(values, "loader_map_after_path", context.loader_map_after_path) ||
        !nullable_path_field(values, "legacy_state_path", context.legacy_state_path) ||
        !nullable_path_field(values, "legacy_deviation_path", context.legacy_deviation_path) ||
        !nullable_path_field(values, "legacy_timing_path", context.legacy_timing_path)) {
      result.status = CP2RuntimeContextStatus::kWrongType;
      return result;
    }
    context.sequence_index = sequence_index.integer;
    context.shadow_enabled = shadow_enabled.boolean;
    if (trace_level == "recorded_full") {
      context.trace_level = CP2RuntimeTraceLevel::kRecordedFull;
    } else if (trace_level == "sequence") {
      context.trace_level = CP2RuntimeTraceLevel::kSequence;
    } else if (trace_level == "timing") {
      context.trace_level = CP2RuntimeTraceLevel::kTiming;
    } else {
      result.status = CP2RuntimeContextStatus::kInvalidValue;
      return result;
    }
    if (schema_version.integer != 1U ||
        record_type != "cp2_runtime_context" || !safe_id(context.run_id) ||
        !expected_sequence(context.sequence_index, context.sequence_id) ||
        !lowercase_hex(context.source_commit, 40U) ||
        !lowercase_hex(context.config_sha256, 64U) ||
        !lowercase_hex(context.bag_sha256, 64U) ||
        !lowercase_hex(context.resolved_parameters_sha256, 64U)) {
      result.status = CP2RuntimeContextStatus::kInvalidValue;
      return result;
    }
    if (!same_expectation(context, expectation)) {
      result.status = CP2RuntimeContextStatus::kExpectationMismatch;
      return result;
    }
    if (!validate_output_paths(context)) {
      result.status = CP2RuntimeContextStatus::kInvalidPath;
      return result;
    }
    if (!validate_output_combination(context)) {
      result.status = CP2RuntimeContextStatus::kInvalidOutputCombination;
      return result;
    }
    CP2Sha256 digest;
    digest.Update(document);
    context.document_sha256 = digest.HexDigest();
    result.context = std::move(context);
    result.status = CP2RuntimeContextStatus::kAccepted;
    return result;
  } catch (...) {
    result = CP2RuntimeContextResult{};
    result.status = CP2RuntimeContextStatus::kAllocationFailure;
    return result;
  }
}
