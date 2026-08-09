/*
 * SchurVIO-Lite CP2 detached offline replay entry point.
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include "CP2OfflineReplay.h"
#include "CP2OfflineReplayInternal.inc"

#include "CP2Canonical.h"
#include "CP2CommitOracle.h"
#include "CP2CompositeState.h"
#include "CP2StateTraceCodec.h"
#include "CP2TraceCodec.h"

#include <boost/math/distributions/chi_squared.hpp>

#include <Eigen/Core>

#include <algorithm>
#include <array>
#include <cerrno>
#include <cfenv>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <fcntl.h>
#include <iomanip>
#include <limits>
#include <locale>
#include <map>
#include <new>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <sys/stat.h>
#include <unistd.h>
#include <tuple>
#include <utility>
#include <vector>

namespace {

using ov_msckf::CP2OfflineReplayResult;
using ov_msckf::CP2OfflineReplayStatus;

constexpr std::uint64_t kMaximumJsonBytes = UINT64_C(1073741824);
constexpr std::uint64_t kMaximumBinaryBytes = UINT64_C(2147483648);
constexpr std::size_t kMaximumJsonDepth = 64U;
constexpr std::uint64_t kMaximumJsonNodes = UINT64_C(50000000);

class ReplayError final : public std::runtime_error {
public:
  ReplayError(CP2OfflineReplayStatus status, const std::string &message)
      : std::runtime_error(message), status_(status) {}

  CP2OfflineReplayStatus status() const noexcept { return status_; }

private:
  CP2OfflineReplayStatus status_;
};

class OwnedDescriptor {
public:
  OwnedDescriptor() noexcept = default;
  explicit OwnedDescriptor(int descriptor) noexcept : descriptor_(descriptor) {}
  ~OwnedDescriptor() {
    if (descriptor_ >= 0) {
      (void)::close(descriptor_);
    }
  }
  OwnedDescriptor(const OwnedDescriptor &) = delete;
  OwnedDescriptor &operator=(const OwnedDescriptor &) = delete;
  OwnedDescriptor(OwnedDescriptor &&other) noexcept
      : descriptor_(other.release()) {}
  OwnedDescriptor &operator=(OwnedDescriptor &&other) noexcept {
    if (this != &other) {
      if (descriptor_ >= 0) {
        (void)::close(descriptor_);
      }
      descriptor_ = other.release();
    }
    return *this;
  }

  int get() const noexcept { return descriptor_; }
  bool valid() const noexcept { return descriptor_ >= 0; }
  int release() noexcept {
    const int result = descriptor_;
    descriptor_ = -1;
    return result;
  }

private:
  int descriptor_ = -1;
};

bool checked_add(std::uint64_t left, std::uint64_t right,
                 std::uint64_t &output) noexcept {
  return ov_msckf::cp2_checked_add_u64(left, right, output);
}

bool valid_sha256(const std::string &value) noexcept {
  if (value.size() != 64U) {
    return false;
  }
  for (char byte : value) {
    if (!((byte >= '0' && byte <= '9') || (byte >= 'a' && byte <= 'f'))) {
      return false;
    }
  }
  return true;
}

bool valid_git_identity(const std::string &value) noexcept {
  if (value.size() != 40U) {
    return false;
  }
  for (char byte : value) {
    if (!((byte >= '0' && byte <= '9') || (byte >= 'a' && byte <= 'f'))) {
      return false;
    }
  }
  return true;
}

bool normalized_absolute_path(const std::string &path) noexcept {
  if (path.size() < 2U || path.front() != '/' || path.back() == '/' ||
      path.find('\0') != std::string::npos) {
    return false;
  }
  std::size_t begin = 1U;
  while (begin < path.size()) {
    const std::size_t end = path.find('/', begin);
    const std::size_t final = end == std::string::npos ? path.size() : end;
    const std::size_t length = final - begin;
    if (length == 0U ||
        (length == 1U && path[begin] == '.') ||
        (length == 2U && path[begin] == '.' && path[begin + 1U] == '.')) {
      return false;
    }
    if (end == std::string::npos) {
      return true;
    }
    begin = end + 1U;
  }
  return false;
}

bool safe_relative_path(const std::string &path) noexcept {
  if (path.empty() || path.front() == '/' || path.back() == '/' ||
      path.find('\0') != std::string::npos || path.find('\\') != std::string::npos) {
    return false;
  }
  std::size_t begin = 0U;
  while (begin < path.size()) {
    const std::size_t end = path.find('/', begin);
    const std::size_t final = end == std::string::npos ? path.size() : end;
    const std::size_t length = final - begin;
    if (length == 0U ||
        (length == 1U && path[begin] == '.') ||
        (length == 2U && path[begin] == '.' && path[begin + 1U] == '.')) {
      return false;
    }
    if (end == std::string::npos) {
      return true;
    }
    begin = end + 1U;
  }
  return false;
}

std::vector<std::string> path_components(const std::string &path,
                                         bool absolute) {
  std::vector<std::string> result;
  std::size_t begin = absolute ? 1U : 0U;
  while (begin < path.size()) {
    const std::size_t end = path.find('/', begin);
    const std::size_t final = end == std::string::npos ? path.size() : end;
    result.push_back(path.substr(begin, final - begin));
    if (end == std::string::npos) {
      break;
    }
    begin = end + 1U;
  }
  return result;
}

OwnedDescriptor open_absolute(const std::string &path, int leaf_flags,
                              bool leaf_directory) {
  if (!normalized_absolute_path(path)) {
    throw ReplayError(CP2OfflineReplayStatus::kInvalidPath,
                      "path is not normalized absolute");
  }
  OwnedDescriptor current(::open("/", O_RDONLY | O_DIRECTORY | O_CLOEXEC));
  if (!current.valid()) {
    throw ReplayError(CP2OfflineReplayStatus::kInvalidPath,
                      "cannot open filesystem root");
  }
  const std::vector<std::string> components = path_components(path, true);
  for (std::size_t index = 0U; index < components.size(); ++index) {
    const bool leaf = index + 1U == components.size();
    int flags = O_CLOEXEC | O_NOFOLLOW;
    if (!leaf || leaf_directory) {
      flags |= O_DIRECTORY;
    }
    flags |= leaf ? leaf_flags : O_RDONLY;
    const int descriptor = ::openat(current.get(), components[index].c_str(), flags);
    if (descriptor < 0) {
      throw ReplayError(CP2OfflineReplayStatus::kInvalidPath,
                        "path component cannot be opened without following links");
    }
    current = OwnedDescriptor(descriptor);
  }
  return current;
}

OwnedDescriptor open_relative_file(int directory, const std::string &path,
                                   int leaf_flags) {
  if (directory < 0 || !safe_relative_path(path)) {
    throw ReplayError(CP2OfflineReplayStatus::kInvalidArtifact,
                      "artifact relative path is unsafe");
  }
  const int duplicate = ::fcntl(directory, F_DUPFD_CLOEXEC, 3);
  if (duplicate < 0) {
    throw ReplayError(CP2OfflineReplayStatus::kInvalidArtifact,
                      "cannot duplicate artifact directory");
  }
  OwnedDescriptor current(duplicate);
  const std::vector<std::string> components = path_components(path, false);
  for (std::size_t index = 0U; index < components.size(); ++index) {
    const bool leaf = index + 1U == components.size();
    int flags = O_CLOEXEC | O_NOFOLLOW | (leaf ? leaf_flags : O_RDONLY);
    if (!leaf) {
      flags |= O_DIRECTORY;
    }
    const int descriptor = ::openat(current.get(), components[index].c_str(), flags);
    if (descriptor < 0) {
      throw ReplayError(CP2OfflineReplayStatus::kInvalidArtifact,
                        "artifact file cannot be opened without following links");
    }
    current = OwnedDescriptor(descriptor);
  }
  return current;
}

void require_regular_single(int descriptor, bool require_empty) {
  struct stat status {};
  if (descriptor < 0 || ::fstat(descriptor, &status) != 0 ||
      !S_ISREG(status.st_mode) || status.st_nlink != 1 || status.st_size < 0 ||
      (require_empty && status.st_size != 0)) {
    throw ReplayError(require_empty ? CP2OfflineReplayStatus::kOutputFailure
                                    : CP2OfflineReplayStatus::kInvalidArtifact,
                      "file is not a regular single-link file with the required size");
  }
}

struct FrozenMetadata {
  dev_t device = 0;
  ino_t inode = 0;
  off_t size = 0;
  timespec modified{};
  timespec changed{};
};

FrozenMetadata metadata(int descriptor) {
  struct stat status {};
  if (::fstat(descriptor, &status) != 0 || !S_ISREG(status.st_mode) ||
      status.st_nlink != 1 || status.st_size < 0) {
    throw ReplayError(CP2OfflineReplayStatus::kInvalidArtifact,
                      "artifact input metadata is invalid");
  }
  FrozenMetadata result;
  result.device = status.st_dev;
  result.inode = status.st_ino;
  result.size = status.st_size;
#if defined(__APPLE__)
  result.modified = status.st_mtimespec;
  result.changed = status.st_ctimespec;
#else
  result.modified = status.st_mtim;
  result.changed = status.st_ctim;
#endif
  return result;
}

bool same_metadata(const FrozenMetadata &left,
                   const FrozenMetadata &right) noexcept {
  return left.device == right.device && left.inode == right.inode &&
         left.size == right.size &&
         left.modified.tv_sec == right.modified.tv_sec &&
         left.modified.tv_nsec == right.modified.tv_nsec &&
         left.changed.tv_sec == right.changed.tv_sec &&
         left.changed.tv_nsec == right.changed.tv_nsec;
}

struct FileBytes {
  std::vector<std::uint8_t> bytes;
  std::string sha256;
};

FileBytes read_file(int descriptor, std::uint64_t maximum_bytes) {
  require_regular_single(descriptor, false);
  const FrozenMetadata before = metadata(descriptor);
  if (static_cast<std::uint64_t>(before.size) > maximum_bytes ||
      static_cast<std::uint64_t>(before.size) >
          static_cast<std::uint64_t>(std::numeric_limits<std::size_t>::max())) {
    throw ReplayError(CP2OfflineReplayStatus::kInvalidArtifact,
                      "artifact input exceeds its bounded size");
  }
  if (::lseek(descriptor, 0, SEEK_SET) != 0) {
    throw ReplayError(CP2OfflineReplayStatus::kInvalidArtifact,
                      "artifact input is not seekable");
  }
  FileBytes result;
  result.bytes.resize(static_cast<std::size_t>(before.size));
  std::size_t offset = 0U;
  while (offset < result.bytes.size()) {
    ssize_t amount = -1;
    do {
      amount = ::read(descriptor, result.bytes.data() + offset,
                      result.bytes.size() - offset);
    } while (amount < 0 && errno == EINTR);
    if (amount <= 0) {
      throw ReplayError(CP2OfflineReplayStatus::kInvalidArtifact,
                        "artifact input changed or could not be read completely");
    }
    offset += static_cast<std::size_t>(amount);
  }
  std::uint8_t extra = 0U;
  ssize_t trailing = -1;
  do {
    trailing = ::read(descriptor, &extra, 1U);
  } while (trailing < 0 && errno == EINTR);
  if (trailing != 0 || !same_metadata(before, metadata(descriptor))) {
    throw ReplayError(CP2OfflineReplayStatus::kInvalidArtifact,
                      "artifact input mutated while it was read");
  }
  ov_msckf::CP2Sha256 sha256;
  sha256.Update(result.bytes);
  result.sha256 = sha256.HexDigest();
  return result;
}

FileBytes read_artifact_file(int directory, const std::string &path,
                             std::uint64_t maximum_bytes) {
  OwnedDescriptor descriptor = open_relative_file(directory, path, O_RDONLY);
  return read_file(descriptor.get(), maximum_bytes);
}

std::string bytes_string(const std::vector<std::uint8_t> &bytes) {
  return std::string(bytes.begin(), bytes.end());
}

bool is_continuation(unsigned char value) noexcept {
  return (value & UINT8_C(0xc0)) == UINT8_C(0x80);
}

bool valid_utf8(const std::string &value) noexcept {
  std::size_t index = 0U;
  while (index < value.size()) {
    const unsigned char first = static_cast<unsigned char>(value[index]);
    if (first <= UINT8_C(0x7f)) {
      ++index;
    } else if (first >= UINT8_C(0xc2) && first <= UINT8_C(0xdf)) {
      if (index + 1U >= value.size() ||
          !is_continuation(static_cast<unsigned char>(value[index + 1U]))) {
        return false;
      }
      index += 2U;
    } else if (first >= UINT8_C(0xe0) && first <= UINT8_C(0xef)) {
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
    } else if (first >= UINT8_C(0xf0) && first <= UINT8_C(0xf4)) {
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
    } else {
      return false;
    }
  }
  return true;
}

void append_utf8_codepoint(std::uint32_t value, std::string &output) {
  if (value <= UINT32_C(0x7f)) {
    output.push_back(static_cast<char>(value));
  } else if (value <= UINT32_C(0x7ff)) {
    output.push_back(static_cast<char>(UINT32_C(0xc0) | (value >> 6U)));
    output.push_back(
        static_cast<char>(UINT32_C(0x80) | (value & UINT32_C(0x3f))));
  } else if (value <= UINT32_C(0xffff)) {
    output.push_back(static_cast<char>(UINT32_C(0xe0) | (value >> 12U)));
    output.push_back(static_cast<char>(UINT32_C(0x80) |
                                       ((value >> 6U) & UINT32_C(0x3f))));
    output.push_back(
        static_cast<char>(UINT32_C(0x80) | (value & UINT32_C(0x3f))));
  } else {
    output.push_back(static_cast<char>(UINT32_C(0xf0) | (value >> 18U)));
    output.push_back(static_cast<char>(UINT32_C(0x80) |
                                       ((value >> 12U) & UINT32_C(0x3f))));
    output.push_back(static_cast<char>(UINT32_C(0x80) |
                                       ((value >> 6U) & UINT32_C(0x3f))));
    output.push_back(
        static_cast<char>(UINT32_C(0x80) | (value & UINT32_C(0x3f))));
  }
}

enum class JsonKind : std::uint8_t {
  kNull,
  kBool,
  kU64,
  kI64,
  kDouble,
  kString,
  kArray,
  kObject,
};

struct JsonValue {
  JsonKind kind = JsonKind::kNull;
  bool boolean = false;
  std::uint64_t unsigned_integer = 0U;
  std::int64_t signed_integer = 0;
  double floating = 0.0;
  std::string string;
  std::vector<JsonValue> array;
  std::map<std::string, JsonValue> object;
};

class StrictJsonParser {
public:
  explicit StrictJsonParser(const std::string &input) : input_(input) {}

  JsonValue Parse() {
    SkipWhitespace();
    JsonValue value = ParseValue(0U);
    SkipWhitespace();
    if (position_ != input_.size()) {
      Fail("JSON has trailing bytes");
    }
    return value;
  }

private:
  [[noreturn]] void Fail(const char *message) const {
    throw ReplayError(CP2OfflineReplayStatus::kInvalidArtifact, message);
  }

  void CountNode() {
    if (nodes_ == kMaximumJsonNodes) {
      Fail("JSON node population exceeds the replay bound");
    }
    ++nodes_;
  }

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

  std::uint32_t ParseHex4() {
    if (position_ > input_.size() || input_.size() - position_ < 4U) {
      Fail("JSON has a truncated unicode escape");
    }
    std::uint32_t value = 0U;
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
        Fail("JSON unicode escape is not hexadecimal");
      }
      value = (value << 4U) | digit;
    }
    position_ += 4U;
    return value;
  }

  std::string ParseString() {
    if (!Take('"')) {
      Fail("JSON string opening quote is missing");
    }
    std::string output;
    while (position_ < input_.size()) {
      const unsigned char byte = static_cast<unsigned char>(input_[position_++]);
      if (byte == static_cast<unsigned char>('"')) {
        if (!valid_utf8(output)) {
          Fail("JSON string is not valid UTF-8");
        }
        return output;
      }
      if (byte < UINT8_C(0x20)) {
        Fail("JSON string contains an unescaped control byte");
      }
      if (byte != static_cast<unsigned char>('\\')) {
        output.push_back(static_cast<char>(byte));
        continue;
      }
      if (position_ >= input_.size()) {
        Fail("JSON string has a truncated escape");
      }
      switch (input_[position_++]) {
      case '"': output.push_back('"'); break;
      case '\\': output.push_back('\\'); break;
      case '/': output.push_back('/'); break;
      case 'b': output.push_back('\b'); break;
      case 'f': output.push_back('\f'); break;
      case 'n': output.push_back('\n'); break;
      case 'r': output.push_back('\r'); break;
      case 't': output.push_back('\t'); break;
      case 'u': {
        const std::uint32_t first = ParseHex4();
        std::uint32_t codepoint = first;
        if (first >= UINT32_C(0xd800) && first <= UINT32_C(0xdbff)) {
          if (position_ + 2U > input_.size() || input_[position_] != '\\' ||
              input_[position_ + 1U] != 'u') {
            Fail("JSON high surrogate lacks a low surrogate");
          }
          position_ += 2U;
          const std::uint32_t second = ParseHex4();
          if (second < UINT32_C(0xdc00) || second > UINT32_C(0xdfff)) {
            Fail("JSON low surrogate is invalid");
          }
          codepoint = UINT32_C(0x10000) +
                      ((first - UINT32_C(0xd800)) << 10U) +
                      (second - UINT32_C(0xdc00));
        } else if (first >= UINT32_C(0xdc00) &&
                   first <= UINT32_C(0xdfff)) {
          Fail("JSON contains an unpaired low surrogate");
        }
        append_utf8_codepoint(codepoint, output);
        break;
      }
      default:
        Fail("JSON string escape is invalid");
      }
    }
    Fail("JSON string is unterminated");
  }

  JsonValue ParseNumber() {
    const std::size_t begin = position_;
    const bool negative = Take('-');
    if (position_ >= input_.size()) {
      Fail("JSON number is truncated");
    }
    if (input_[position_] == '0') {
      ++position_;
      if (position_ < input_.size() && input_[position_] >= '0' &&
          input_[position_] <= '9') {
        Fail("JSON number has a leading zero");
      }
    } else if (input_[position_] >= '1' && input_[position_] <= '9') {
      while (position_ < input_.size() && input_[position_] >= '0' &&
             input_[position_] <= '9') {
        ++position_;
      }
    } else {
      Fail("JSON number has no integer part");
    }
    bool floating = false;
    if (Take('.')) {
      floating = true;
      const std::size_t fraction = position_;
      while (position_ < input_.size() && input_[position_] >= '0' &&
             input_[position_] <= '9') {
        ++position_;
      }
      if (fraction == position_) {
        Fail("JSON number has an empty fraction");
      }
    }
    if (position_ < input_.size() &&
        (input_[position_] == 'e' || input_[position_] == 'E')) {
      floating = true;
      ++position_;
      if (position_ < input_.size() &&
          (input_[position_] == '+' || input_[position_] == '-')) {
        ++position_;
      }
      const std::size_t exponent = position_;
      while (position_ < input_.size() && input_[position_] >= '0' &&
             input_[position_] <= '9') {
        ++position_;
      }
      if (exponent == position_) {
        Fail("JSON number has an empty exponent");
      }
    }
    const std::string token = input_.substr(begin, position_ - begin);
    JsonValue result;
    if (floating) {
      std::istringstream stream(token);
      stream.imbue(std::locale::classic());
      stream >> std::noskipws >> result.floating;
      if (!stream || !stream.eof() || !std::isfinite(result.floating)) {
        Fail("JSON floating value is not finite binary64");
      }
      result.kind = JsonKind::kDouble;
      return result;
    }
    std::uint64_t magnitude = 0U;
    const std::size_t digit_begin = negative ? begin + 1U : begin;
    for (std::size_t index = digit_begin; index < position_; ++index) {
      const std::uint64_t digit =
          static_cast<std::uint64_t>(input_[index] - '0');
      if (magnitude >
          (std::numeric_limits<std::uint64_t>::max() - digit) / 10U) {
        Fail("JSON integer exceeds u64");
      }
      magnitude = magnitude * 10U + digit;
    }
    if (!negative) {
      result.kind = JsonKind::kU64;
      result.unsigned_integer = magnitude;
      return result;
    }
    const std::uint64_t minimum_magnitude =
        UINT64_C(1) << (std::numeric_limits<std::int64_t>::digits);
    if (magnitude > minimum_magnitude) {
      Fail("JSON negative integer is below i64");
    }
    result.kind = JsonKind::kI64;
    result.signed_integer =
        magnitude == minimum_magnitude
            ? std::numeric_limits<std::int64_t>::min()
            : -static_cast<std::int64_t>(magnitude);
    return result;
  }

  JsonValue ParseValue(std::size_t depth) {
    if (depth > kMaximumJsonDepth) {
      Fail("JSON nesting exceeds the replay bound");
    }
    CountNode();
    if (position_ >= input_.size()) {
      Fail("JSON value is missing");
    }
    JsonValue result;
    if (input_[position_] == '"') {
      result.kind = JsonKind::kString;
      result.string = ParseString();
      return result;
    }
    if (input_.compare(position_, 4U, "null") == 0) {
      position_ += 4U;
      result.kind = JsonKind::kNull;
      return result;
    }
    if (input_.compare(position_, 4U, "true") == 0) {
      position_ += 4U;
      result.kind = JsonKind::kBool;
      result.boolean = true;
      return result;
    }
    if (input_.compare(position_, 5U, "false") == 0) {
      position_ += 5U;
      result.kind = JsonKind::kBool;
      result.boolean = false;
      return result;
    }
    if (input_[position_] == '-' ||
        (input_[position_] >= '0' && input_[position_] <= '9')) {
      return ParseNumber();
    }
    if (Take('[')) {
      result.kind = JsonKind::kArray;
      SkipWhitespace();
      if (Take(']')) {
        return result;
      }
      while (true) {
        result.array.push_back(ParseValue(depth + 1U));
        SkipWhitespace();
        if (Take(']')) {
          return result;
        }
        if (!Take(',')) {
          Fail("JSON array delimiter is invalid");
        }
        SkipWhitespace();
      }
    }
    if (Take('{')) {
      result.kind = JsonKind::kObject;
      SkipWhitespace();
      if (Take('}')) {
        return result;
      }
      while (true) {
        if (position_ >= input_.size() || input_[position_] != '"') {
          Fail("JSON object key is not a string");
        }
        const std::string key = ParseString();
        SkipWhitespace();
        if (!Take(':')) {
          Fail("JSON object key lacks a colon");
        }
        SkipWhitespace();
        JsonValue value = ParseValue(depth + 1U);
        if (!result.object.emplace(key, std::move(value)).second) {
          Fail("JSON object contains a duplicate key");
        }
        SkipWhitespace();
        if (Take('}')) {
          return result;
        }
        if (!Take(',')) {
          Fail("JSON object delimiter is invalid");
        }
        SkipWhitespace();
      }
    }
    Fail("JSON value token is invalid");
  }

  const std::string &input_;
  std::size_t position_ = 0U;
  std::uint64_t nodes_ = 0U;
};

JsonValue parse_json(const FileBytes &file) {
  return StrictJsonParser(bytes_string(file.bytes)).Parse();
}

std::vector<JsonValue> parse_json_lines(const FileBytes &file) {
  const std::string input = bytes_string(file.bytes);
  if (input.empty() || input.back() != '\n') {
    throw ReplayError(CP2OfflineReplayStatus::kInvalidArtifact,
                      "JSONL input must be nonempty and newline terminated");
  }
  std::vector<JsonValue> result;
  std::size_t beginning = 0U;
  while (beginning < input.size()) {
    const std::size_t end = input.find('\n', beginning);
    if (end == std::string::npos || end == beginning ||
        (end > beginning && input[end - 1U] == '\r')) {
      throw ReplayError(CP2OfflineReplayStatus::kInvalidArtifact,
                        "JSONL input contains an empty, CRLF, or truncated row");
    }
    result.push_back(
        StrictJsonParser(input.substr(beginning, end - beginning)).Parse());
    beginning = end + 1U;
  }
  return result;
}

const std::map<std::string, JsonValue> &as_object(const JsonValue &value,
                                                  const char *what) {
  if (value.kind != JsonKind::kObject) {
    throw ReplayError(CP2OfflineReplayStatus::kInvalidArtifact,
                      std::string(what) + " is not an object");
  }
  return value.object;
}

const std::vector<JsonValue> &as_array(const JsonValue &value,
                                       const char *what) {
  if (value.kind != JsonKind::kArray) {
    throw ReplayError(CP2OfflineReplayStatus::kInvalidArtifact,
                      std::string(what) + " is not an array");
  }
  return value.array;
}

const JsonValue &required(const std::map<std::string, JsonValue> &object,
                          const std::string &key) {
  const auto found = object.find(key);
  if (found == object.end()) {
    throw ReplayError(CP2OfflineReplayStatus::kInvalidArtifact,
                      "JSON object lacks required key " + key);
  }
  return found->second;
}

std::string as_string(const JsonValue &value, const char *what) {
  if (value.kind != JsonKind::kString) {
    throw ReplayError(CP2OfflineReplayStatus::kInvalidArtifact,
                      std::string(what) + " is not a string");
  }
  return value.string;
}

std::uint64_t as_u64(const JsonValue &value, const char *what) {
  if (value.kind != JsonKind::kU64) {
    throw ReplayError(CP2OfflineReplayStatus::kInvalidArtifact,
                      std::string(what) + " is not u64");
  }
  return value.unsigned_integer;
}

bool as_bool(const JsonValue &value, const char *what) {
  if (value.kind != JsonKind::kBool) {
    throw ReplayError(CP2OfflineReplayStatus::kInvalidArtifact,
                      std::string(what) + " is not Boolean");
  }
  return value.boolean;
}

double as_double(const JsonValue &value, const char *what) {
  if (value.kind == JsonKind::kDouble) {
    return value.floating;
  }
  if (value.kind == JsonKind::kU64 &&
      value.unsigned_integer <= UINT64_C(9007199254740992)) {
    return static_cast<double>(value.unsigned_integer);
  }
  if (value.kind == JsonKind::kI64 &&
      value.signed_integer >= INT64_C(-9007199254740992) &&
      value.signed_integer <= INT64_C(9007199254740992)) {
    return static_cast<double>(value.signed_integer);
  }
  throw ReplayError(CP2OfflineReplayStatus::kInvalidArtifact,
                    std::string(what) + " is not an exactly representable f64");
}

bool nullable(const JsonValue &value) noexcept {
  return value.kind == JsonKind::kNull;
}

bool binary64_equal(double left, double right) noexcept;

void require_exact_keys(const std::map<std::string, JsonValue> &object,
                        const std::vector<std::string> &keys,
                        const char *what) {
  if (object.size() != keys.size()) {
    throw ReplayError(CP2OfflineReplayStatus::kInvalidArtifact,
                      std::string(what) + " has a noncanonical key inventory");
  }
  for (const std::string &key : keys) {
    if (object.count(key) != 1U) {
      throw ReplayError(CP2OfflineReplayStatus::kInvalidArtifact,
                        std::string(what) + " lacks exact key " + key);
    }
  }
}

void require_schema_record(const std::map<std::string, JsonValue> &object,
                           const char *record_type) {
  if (as_u64(required(object, "schema_version"), "schema_version") != 1U ||
      as_string(required(object, "record_type"), "record_type") !=
          record_type) {
    throw ReplayError(CP2OfflineReplayStatus::kInvalidArtifact,
                      "JSON record schema/type is invalid");
  }
}

std::vector<std::uint64_t> as_u64_array(const JsonValue &value,
                                        const char *what) {
  std::vector<std::uint64_t> result;
  const std::vector<JsonValue> &array = as_array(value, what);
  result.reserve(array.size());
  std::set<std::uint64_t> unique;
  for (const JsonValue &item : array) {
    const std::uint64_t converted = as_u64(item, what);
    if (!unique.insert(converted).second) {
      throw ReplayError(CP2OfflineReplayStatus::kInvalidArtifact,
                        std::string(what) + " contains a duplicate u64");
    }
    result.push_back(converted);
  }
  return result;
}

bool nullable_double_equal(const JsonValue &value, bool available,
                           double expected) {
  if (!available) {
    return nullable(value);
  }
  return !nullable(value) && binary64_equal(as_double(value, "f64"), expected);
}

bool nullable_bool_equal(const JsonValue &value, bool available,
                         bool expected) {
  return available ? (!nullable(value) && as_bool(value, "Boolean") == expected)
                   : nullable(value);
}

bool nullable_u64_equal(const JsonValue &value, bool available,
                        std::uint64_t expected) {
  return available ? (!nullable(value) && as_u64(value, "u64") == expected)
                   : nullable(value);
}

bool nullable_string_equal(const JsonValue &value, bool available,
                           const std::string &expected) {
  return available
             ? (!nullable(value) && as_string(value, "string") == expected)
             : nullable(value);
}

bool binary64_equal(double left, double right) noexcept {
  std::uint64_t left_bits = 0U;
  std::uint64_t right_bits = 0U;
  std::memcpy(&left_bits, &left, sizeof(left_bits));
  std::memcpy(&right_bits, &right, sizeof(right_bits));
  return left_bits == right_bits;
}

std::string json_string(const std::string &value) {
  std::ostringstream output;
  output.imbue(std::locale::classic());
  static constexpr char kHex[] = "0123456789abcdef";
  output << '"';
  for (unsigned char byte : value) {
    switch (byte) {
    case '"': output << "\\\""; break;
    case '\\': output << "\\\\"; break;
    case '\b': output << "\\b"; break;
    case '\f': output << "\\f"; break;
    case '\n': output << "\\n"; break;
    case '\r': output << "\\r"; break;
    case '\t': output << "\\t"; break;
    default:
      if (byte < 0x20U) {
        output << "\\u00" << kHex[(byte >> 4U) & 0x0fU]
               << kHex[byte & 0x0fU];
      } else {
        output << static_cast<char>(byte);
      }
    }
  }
  output << '"';
  return output.str();
}

void increment(std::uint64_t &value, const char *what) {
  std::uint64_t next = 0U;
  if (!checked_add(value, UINT64_C(1), next)) {
    throw ReplayError(CP2OfflineReplayStatus::kReplayFailed,
                      std::string(what) + " count overflows u64");
  }
  value = next;
}

struct Identity {
  std::uint64_t sequence_index = 0U;
  std::uint64_t pair_index = 0U;
  std::uint64_t camera_timestamp_ns = 0U;
  std::uint64_t invocation_id = 0U;

  bool operator<(const Identity &other) const noexcept {
    return std::tie(sequence_index, pair_index, invocation_id,
                    camera_timestamp_ns) <
           std::tie(other.sequence_index, other.pair_index,
                    other.invocation_id, other.camera_timestamp_ns);
  }
};

bool operator==(const Identity &left, const Identity &right) noexcept {
  return left.sequence_index == right.sequence_index &&
         left.pair_index == right.pair_index &&
         left.camera_timestamp_ns == right.camera_timestamp_ns &&
         left.invocation_id == right.invocation_id;
}

using TraceKey = std::tuple<std::uint64_t, std::uint64_t, std::uint64_t>;

TraceKey trace_key(const Identity &identity) {
  return std::make_tuple(identity.sequence_index, identity.pair_index,
                         identity.invocation_id);
}

TraceKey trace_key(const ov_msckf::CP2InvocationTraceIdentity &identity) {
  return std::make_tuple(identity.sequence_index, identity.pair_index,
                         identity.invocation_id);
}

Identity identity_from_row(const std::map<std::string, JsonValue> &row) {
  Identity result;
  result.sequence_index = as_u64(required(row, "sequence_index"), "sequence_index");
  result.pair_index = as_u64(required(row, "pair_index"), "pair_index");
  result.camera_timestamp_ns =
      as_u64(required(row, "camera_timestamp_ns"), "camera_timestamp_ns");
  result.invocation_id = as_u64(required(row, "invocation_id"), "invocation_id");
  return result;
}

ov_msckf::CP2InvocationTraceIdentity trace_identity(const Identity &identity) {
  ov_msckf::CP2InvocationTraceIdentity result;
  result.sequence_index = identity.sequence_index;
  result.pair_index = identity.pair_index;
  result.invocation_id = identity.invocation_id;
  return result;
}

struct FailureCounts {
  std::uint64_t layout = 0U;
  std::uint64_t reduction = 0U;
  std::uint64_t statistics = 0U;
  std::uint64_t gate = 0U;
  std::uint64_t accepted_sequence = 0U;
  std::uint64_t gamma = 0U;
  std::uint64_t stack = 0U;
  std::uint64_t compression = 0U;
  std::uint64_t preview_status = 0U;
  std::uint64_t proposal_presence = 0U;
  std::uint64_t proposal_bytes = 0U;
  std::uint64_t commit_oracle = 0U;
  std::uint64_t block_metrics = 0U;

  bool empty() const noexcept {
    return layout == 0U && reduction == 0U && statistics == 0U && gate == 0U &&
           accepted_sequence == 0U && gamma == 0U && stack == 0U &&
           compression == 0U && preview_status == 0U &&
           proposal_presence == 0U && proposal_bytes == 0U &&
           commit_oracle == 0U && block_metrics == 0U;
  }
};

struct ResolvedParameterDigest {
  std::uint64_t sequence_index = 0U;
  std::string sha256;
  double sigma_px = 0.0;
  double chi2_multiplier = 0.0;
};

bool bytewise_less(const std::string &left, const std::string &right) noexcept {
  const std::size_t count = std::min(left.size(), right.size());
  for (std::size_t index = 0U; index < count; ++index) {
    const unsigned char left_byte = static_cast<unsigned char>(left[index]);
    const unsigned char right_byte = static_cast<unsigned char>(right[index]);
    if (left_byte != right_byte) {
      return left_byte < right_byte;
    }
  }
  return left.size() < right.size();
}

bool ends_with(const std::string &value, const char *suffix) noexcept {
  const std::size_t suffix_size = std::strlen(suffix);
  return value.size() >= suffix_size &&
         value.compare(value.size() - suffix_size, suffix_size, suffix) == 0;
}

std::uint64_t elf_integer(const std::vector<std::uint8_t> &bytes,
                          std::uint64_t offset, std::size_t width,
                          bool little_endian) {
  if ((width != 2U && width != 4U && width != 8U) ||
      offset > bytes.size() || width > bytes.size() - offset) {
    throw ReplayError(CP2OfflineReplayStatus::kInvalidArtifact,
                      "executable ELF metadata is truncated");
  }
  std::uint64_t result = 0U;
  if (little_endian) {
    for (std::size_t index = 0U; index < width; ++index) {
      result |= static_cast<std::uint64_t>(
                    bytes[static_cast<std::size_t>(offset) + index])
                << (8U * index);
    }
  } else {
    for (std::size_t index = 0U; index < width; ++index) {
      result = (result << 8U) |
               bytes[static_cast<std::size_t>(offset) + index];
    }
  }
  return result;
}

std::uint64_t align4(std::uint64_t value) {
  if (value > std::numeric_limits<std::uint64_t>::max() - 3U) {
    throw ReplayError(CP2OfflineReplayStatus::kInvalidArtifact,
                      "executable ELF note length overflows");
  }
  return (value + 3U) & ~UINT64_C(3);
}

std::string executable_build_id(const std::vector<std::uint8_t> &bytes) {
  if (bytes.size() < 64U || bytes[0] != UINT8_C(0x7f) || bytes[1] != 'E' ||
      bytes[2] != 'L' || bytes[3] != 'F' ||
      (bytes[4] != 1U && bytes[4] != 2U) ||
      (bytes[5] != 1U && bytes[5] != 2U)) {
    throw ReplayError(CP2OfflineReplayStatus::kInvalidArtifact,
                      "running executable is not a supported ELF image");
  }
  const bool elf64 = bytes[4] == 2U;
  const bool little_endian = bytes[5] == 1U;
  const std::uint64_t phoff = elf_integer(
      bytes, elf64 ? 32U : 28U, elf64 ? 8U : 4U, little_endian);
  const std::uint64_t phentsize =
      elf_integer(bytes, elf64 ? 54U : 42U, 2U, little_endian);
  const std::uint64_t phnum =
      elf_integer(bytes, elf64 ? 56U : 44U, 2U, little_endian);
  const std::uint64_t minimum_entry = elf64 ? 56U : 32U;
  if (phnum == 0U || phnum == UINT16_C(0xffff) ||
      phentsize < minimum_entry || phnum > UINT64_C(1000000)) {
    throw ReplayError(CP2OfflineReplayStatus::kInvalidArtifact,
                      "executable ELF program-header table is unsupported");
  }
  std::string found;
  std::uint64_t found_count = 0U;
  for (std::uint64_t index = 0U; index < phnum; ++index) {
    std::uint64_t scaled = 0U;
    std::uint64_t header = 0U;
    if (!ov_msckf::cp2_checked_multiply_u64(index, phentsize, scaled) ||
        !checked_add(phoff, scaled, header) || header > bytes.size() ||
        minimum_entry > bytes.size() - header) {
      throw ReplayError(CP2OfflineReplayStatus::kInvalidArtifact,
                        "executable ELF program-header table is truncated");
    }
    if (elf_integer(bytes, header, 4U, little_endian) != 4U) {
      continue;
    }
    const std::uint64_t note_offset = elf_integer(
        bytes, header + (elf64 ? 8U : 4U), elf64 ? 8U : 4U, little_endian);
    const std::uint64_t note_size = elf_integer(
        bytes, header + (elf64 ? 32U : 16U), elf64 ? 8U : 4U,
        little_endian);
    if (note_offset > bytes.size() || note_size > bytes.size() - note_offset) {
      throw ReplayError(CP2OfflineReplayStatus::kInvalidArtifact,
                        "executable ELF note segment is outside the file");
    }
    std::uint64_t position = note_offset;
    const std::uint64_t end = note_offset + note_size;
    while (position < end) {
      if (end - position < 12U) {
        throw ReplayError(CP2OfflineReplayStatus::kInvalidArtifact,
                          "executable ELF note header is truncated");
      }
      const std::uint64_t name_size =
          elf_integer(bytes, position, 4U, little_endian);
      const std::uint64_t description_size =
          elf_integer(bytes, position + 4U, 4U, little_endian);
      const std::uint64_t type =
          elf_integer(bytes, position + 8U, 4U, little_endian);
      position += 12U;
      const std::uint64_t padded_name = align4(name_size);
      const std::uint64_t padded_description = align4(description_size);
      std::uint64_t description = 0U;
      std::uint64_t next = 0U;
      if (padded_name > end - position ||
          !checked_add(position, padded_name, description) ||
          padded_description > end - description ||
          !checked_add(description, padded_description, next)) {
        throw ReplayError(CP2OfflineReplayStatus::kInvalidArtifact,
                          "executable ELF note payload is truncated");
      }
      const bool gnu_name =
          name_size == 4U && bytes[static_cast<std::size_t>(position)] == 'G' &&
          bytes[static_cast<std::size_t>(position + 1U)] == 'N' &&
          bytes[static_cast<std::size_t>(position + 2U)] == 'U' &&
          bytes[static_cast<std::size_t>(position + 3U)] == 0U;
      if (type == 3U && gnu_name && description_size > 0U) {
        static constexpr char kHex[] = "0123456789abcdef";
        std::string candidate;
        candidate.resize(static_cast<std::size_t>(description_size) * 2U);
        for (std::uint64_t byte_index = 0U; byte_index < description_size;
             ++byte_index) {
          const std::uint8_t byte = bytes[static_cast<std::size_t>(
              description + byte_index)];
          candidate[2U * static_cast<std::size_t>(byte_index)] =
              kHex[byte >> 4U];
          candidate[2U * static_cast<std::size_t>(byte_index) + 1U] =
              kHex[byte & UINT8_C(0x0f)];
        }
        if (!found.empty() && found != candidate) {
          throw ReplayError(CP2OfflineReplayStatus::kInvalidArtifact,
                            "executable contains conflicting GNU build IDs");
        }
        found = std::move(candidate);
        increment(found_count, "GNU build ID note");
      }
      position = next;
    }
  }
  if (found.empty() || found_count != 1U) {
    throw ReplayError(CP2OfflineReplayStatus::kInvalidArtifact,
                      "running executable does not have exactly one GNU build ID");
  }
  return found;
}

class ParameterReader {
public:
  explicit ParameterReader(const std::vector<std::uint8_t> &bytes)
      : bytes_(bytes) {}

  ResolvedParameterDigest Parse(std::uint64_t sequence_index,
                                const std::string &sha256) {
    static const char kDomain[] = "SchurVIO-CP2-ros-params-v1\0";
    Expect(reinterpret_cast<const std::uint8_t *>(kDomain),
           sizeof(kDomain) - 1U, "resolved-parameter domain");
    if (ReadU8("top-level parameter tag") !=
        static_cast<std::uint8_t>('m')) {
      Fail("resolved parameters are not a top-level map");
    }
    const std::uint64_t count = ReadU64("top-level parameter count");
    if (count == 0U || count > UINT64_C(1000000)) {
      Fail("resolved-parameter population is outside its bound");
    }
    std::string previous;
    bool have_previous = false;
    bool have_sigma = false;
    bool have_chi2 = false;
    double sigma = 0.0;
    double chi2 = 0.0;
    for (std::uint64_t index = 0U; index < count; ++index) {
      const std::string key = ReadString("resolved-parameter name");
      if (key.empty() || key.find('\0') != std::string::npos ||
          key.compare(0U, 9U, "/cp2_vio/") != 0 ||
          (have_previous && !bytewise_less(previous, key))) {
        Fail("resolved-parameter names are not canonical flattened CP2 names");
      }
      previous = key;
      have_previous = true;
      const bool sigma_key = ends_with(key, "/up_msckf_sigma_px");
      const bool chi2_key = ends_with(key, "/up_msckf_chi2_multipler");
      double numeric = 0.0;
      const bool numeric_value = ReadValue(0U, numeric);
      if (sigma_key) {
        if (have_sigma || !numeric_value) {
          Fail("MSCKF sigma parameter is duplicate or nonnumeric");
        }
        have_sigma = true;
        sigma = numeric;
      }
      if (chi2_key) {
        if (have_chi2 || !numeric_value) {
          Fail("MSCKF chi2 multiplier is duplicate or nonnumeric");
        }
        have_chi2 = true;
        chi2 = numeric;
      }
    }
    if (position_ != bytes_.size() || !have_sigma || !have_chi2 ||
        !std::isfinite(sigma) || !(sigma > 0.0) ||
        !std::isfinite(sigma * sigma) || !(sigma * sigma > 0.0) ||
        !std::isfinite(chi2)) {
      Fail("resolved parameters are incomplete or invalid for replay");
    }
    ResolvedParameterDigest result;
    result.sequence_index = sequence_index;
    result.sha256 = sha256;
    result.sigma_px = sigma;
    result.chi2_multiplier = chi2;
    return result;
  }

private:
  [[noreturn]] void Fail(const char *message) const {
    throw ReplayError(CP2OfflineReplayStatus::kInvalidArtifact, message);
  }

  void Require(std::size_t count, const char *what) const {
    if (position_ > bytes_.size() || count > bytes_.size() - position_) {
      throw ReplayError(CP2OfflineReplayStatus::kInvalidArtifact,
                        std::string(what) + " is truncated");
    }
  }

  void Expect(const std::uint8_t *expected, std::size_t count,
              const char *what) {
    Require(count, what);
    if (!std::equal(expected, expected + count, bytes_.begin() +
                                               static_cast<std::ptrdiff_t>(position_))) {
      Fail("resolved-parameter domain is invalid");
    }
    position_ += count;
  }

  std::uint8_t ReadU8(const char *what) {
    Require(1U, what);
    return bytes_[position_++];
  }

  std::uint64_t ReadU64(const char *what) {
    Require(8U, what);
    std::uint64_t result = 0U;
    for (std::size_t offset = 0U; offset < 8U; ++offset) {
      result = (result << 8U) | bytes_[position_ + offset];
    }
    position_ += 8U;
    return result;
  }

  std::string ReadString(const char *what) {
    const std::uint64_t length = ReadU64(what);
    if (length > UINT64_C(16777216) ||
        length > static_cast<std::uint64_t>(
                     std::numeric_limits<std::size_t>::max())) {
      Fail("resolved-parameter string exceeds its bound");
    }
    const std::size_t converted = static_cast<std::size_t>(length);
    Require(converted, what);
    const std::string result(
        reinterpret_cast<const char *>(bytes_.data() + position_), converted);
    position_ += converted;
    if (!valid_utf8(result) || result.find('\0') != std::string::npos) {
      Fail("resolved-parameter string is invalid UTF-8");
    }
    return result;
  }

  bool ReadValue(std::size_t depth, double &numeric) {
    if (depth > kMaximumJsonDepth) {
      Fail("resolved-parameter nesting exceeds its bound");
    }
    const std::uint8_t tag = ReadU8("resolved-parameter value tag");
    if (tag == static_cast<std::uint8_t>('b')) {
      const std::uint8_t value = ReadU8("Boolean parameter value");
      if (value > 1U) {
        Fail("Boolean parameter value is invalid");
      }
      return false;
    }
    if (tag == static_cast<std::uint8_t>('i')) {
      const std::uint64_t bits = ReadU64("integer parameter value");
      std::int64_t signed_value = 0;
      std::memcpy(&signed_value, &bits, sizeof(bits));
      if (signed_value < INT64_C(-9007199254740992) ||
          signed_value > INT64_C(9007199254740992)) {
        return false;
      }
      numeric = static_cast<double>(signed_value);
      return true;
    }
    if (tag == static_cast<std::uint8_t>('f')) {
      const std::uint64_t bits = ReadU64("floating parameter value");
      std::memcpy(&numeric, &bits, sizeof(bits));
      if (!std::isfinite(numeric)) {
        Fail("floating parameter value is nonfinite");
      }
      return true;
    }
    if (tag == static_cast<std::uint8_t>('s')) {
      (void)ReadString("string parameter value");
      return false;
    }
    if (tag == static_cast<std::uint8_t>('l')) {
      const std::uint64_t count = ReadU64("list parameter count");
      if (count > UINT64_C(1000000)) {
        Fail("list parameter population exceeds its bound");
      }
      for (std::uint64_t index = 0U; index < count; ++index) {
        double ignored = 0.0;
        (void)ReadValue(depth + 1U, ignored);
      }
      return false;
    }
    if (tag == static_cast<std::uint8_t>('m')) {
      const std::uint64_t count = ReadU64("map parameter count");
      if (count > UINT64_C(1000000)) {
        Fail("map parameter population exceeds its bound");
      }
      std::string previous;
      bool have_previous = false;
      for (std::uint64_t index = 0U; index < count; ++index) {
        const std::string key = ReadString("map parameter key");
        if (have_previous && !bytewise_less(previous, key)) {
          Fail("nested map parameter keys are not strictly ordered");
        }
        previous = key;
        have_previous = true;
        double ignored = 0.0;
        (void)ReadValue(depth + 1U, ignored);
      }
      return false;
    }
    Fail("resolved-parameter value tag is forbidden");
  }

  const std::vector<std::uint8_t> &bytes_;
  std::size_t position_ = 0U;
};

struct ProvenanceData {
  std::string source_commit;
  std::string source_tree;
  std::string executable_sha256;
  std::string executable_build_id;
  std::vector<ResolvedParameterDigest> resolved_parameters;
};

ProvenanceData parse_provenance(int artifact_directory,
                                const FileBytes &provenance) {
  const std::map<std::string, JsonValue> &root =
      as_object(parse_json(provenance), "provenance");
  require_schema_record(root, "provenance");
  if (as_string(required(root, "checkpoint"), "checkpoint") != "CP2-C") {
    throw ReplayError(CP2OfflineReplayStatus::kInvalidArtifact,
                      "provenance checkpoint is not CP2-C");
  }
  ProvenanceData result;
  result.source_commit =
      as_string(required(root, "source_commit"), "source_commit");
  result.source_tree = as_string(required(root, "source_tree"), "source_tree");
  if (!valid_git_identity(result.source_commit) ||
      !valid_git_identity(result.source_tree)) {
    throw ReplayError(CP2OfflineReplayStatus::kInvalidArtifact,
                      "provenance source identities are invalid");
  }

  const auto &unit = as_object(required(root, "unit_anchor"), "unit_anchor");
  if (!as_bool(required(unit, "verified"), "unit anchor verified") ||
      as_string(required(unit, "tested_commit"), "tested_commit") !=
          result.source_commit ||
      as_string(required(unit, "tested_tree"), "tested_tree") !=
          result.source_tree) {
    throw ReplayError(CP2OfflineReplayStatus::kInvalidArtifact,
                      "unit anchor is disconnected from provenance");
  }
  const auto &build = as_object(required(root, "build"), "build");
  if (!as_bool(required(build, "strict_fp_verified"),
               "strict_fp_verified")) {
    throw ReplayError(CP2OfflineReplayStatus::kInvalidArtifact,
                      "artifact build lacks strict-FP verification");
  }
  const auto &runtime = as_object(required(root, "runtime"), "runtime");
  const std::string before = as_string(
      required(runtime, "executable_sha256_before"), "executable SHA before");
  const std::string after = as_string(
      required(runtime, "executable_sha256_after"), "executable SHA after");
  const std::string build_id_before =
      as_string(required(runtime, "build_id_before"), "build ID before");
  const std::string build_id_after =
      as_string(required(runtime, "build_id_after"), "build ID after");
  if (!valid_sha256(before) || before != after || build_id_before.empty() ||
      build_id_before != build_id_after) {
    throw ReplayError(CP2OfflineReplayStatus::kInvalidArtifact,
                      "runtime executable identity is not stable");
  }
  result.executable_sha256 = before;
  result.executable_build_id = build_id_before;

  std::map<std::string, std::uint64_t> runs;
  const std::vector<JsonValue> &run_rows =
      as_array(required(runtime, "runs"), "runtime runs");
  if (run_rows.empty()) {
    throw ReplayError(CP2OfflineReplayStatus::kInvalidArtifact,
                      "runtime run population is empty");
  }
  for (const JsonValue &run_value : run_rows) {
    const auto &run = as_object(run_value, "runtime run");
    const std::string run_id = as_string(required(run, "run_id"), "run_id");
    const std::uint64_t sequence =
        as_u64(required(run, "sequence_index"), "sequence_index");
    if (run_id.empty() || !runs.emplace(run_id, sequence).second) {
      throw ReplayError(CP2OfflineReplayStatus::kInvalidArtifact,
                        "runtime run identities are invalid");
    }
  }

  const auto &configuration =
      as_object(required(root, "configuration"), "configuration");
  const std::vector<JsonValue> &records = as_array(
      required(configuration, "resolved_parameters"), "resolved parameters");
  if (records.size() != runs.size()) {
    throw ReplayError(CP2OfflineReplayStatus::kInvalidArtifact,
                      "resolved parameters do not join one-to-one to runs");
  }
  std::set<std::string> joined_runs;
  std::set<std::uint64_t> joined_sequences;
  for (const JsonValue &record_value : records) {
    const auto &record = as_object(record_value, "resolved parameter record");
    const std::string run_id =
        as_string(required(record, "run_id"), "resolved run_id");
    const auto run = runs.find(run_id);
    if (run == runs.end() || !joined_runs.insert(run_id).second ||
        !joined_sequences.insert(run->second).second) {
      throw ReplayError(CP2OfflineReplayStatus::kInvalidArtifact,
                        "resolved parameter run join is invalid");
    }
    const std::string path =
        as_string(required(record, "canonical_path"), "canonical_path");
    const std::string sha256 = as_string(
        required(record, "canonical_sha256"), "canonical_sha256");
    if (!safe_relative_path(path) || !valid_sha256(sha256)) {
      throw ReplayError(CP2OfflineReplayStatus::kInvalidArtifact,
                        "resolved parameter file reference is invalid");
    }
    const FileBytes canonical =
        read_artifact_file(artifact_directory, path, kMaximumJsonBytes);
    if (canonical.sha256 != sha256) {
      throw ReplayError(CP2OfflineReplayStatus::kInvalidArtifact,
                        "resolved parameter payload hash mismatch");
    }
    result.resolved_parameters.push_back(
        ParameterReader(canonical.bytes).Parse(run->second, sha256));
  }
  std::sort(result.resolved_parameters.begin(),
            result.resolved_parameters.end(),
            [](const ResolvedParameterDigest &left,
               const ResolvedParameterDigest &right) {
              return left.sequence_index < right.sequence_index;
            });
  for (std::size_t index = 0U; index < result.resolved_parameters.size();
       ++index) {
    if (result.resolved_parameters[index].sequence_index != index) {
      throw ReplayError(CP2OfflineReplayStatus::kInvalidArtifact,
                        "resolved parameter sequence indices are not contiguous");
    }
  }
  return result;
}

bool artifact_file_exists(int artifact_directory, const char *name) {
  struct stat status {};
  if (::fstatat(artifact_directory, name, &status, AT_SYMLINK_NOFOLLOW) == 0) {
    return true;
  }
  if (errno == ENOENT) {
    return false;
  }
  throw ReplayError(CP2OfflineReplayStatus::kInvalidArtifact,
                    "artifact metadata path cannot be inspected");
}

ProvenanceData parse_replay_input(int artifact_directory,
                                  const FileBytes &metadata) {
  const auto &root = as_object(parse_json(metadata), "replay input");
  static const std::vector<std::string> root_keys = {
      "schema_version", "record_type", "checkpoint", "source_commit",
      "source_tree", "executable_sha256", "executable_build_id",
      "strict_fp_verified", "resolved_parameters"};
  require_exact_keys(root, root_keys, "replay input");
  require_schema_record(root, "cp2_offline_replay_input");
  ProvenanceData result;
  result.source_commit =
      as_string(required(root, "source_commit"), "source commit");
  result.source_tree = as_string(required(root, "source_tree"), "source tree");
  result.executable_sha256 =
      as_string(required(root, "executable_sha256"), "executable SHA-256");
  result.executable_build_id =
      as_string(required(root, "executable_build_id"), "executable build ID");
  if (as_string(required(root, "checkpoint"), "checkpoint") != "CP2-C" ||
      !as_bool(required(root, "strict_fp_verified"), "strict FP verified") ||
      !valid_git_identity(result.source_commit) ||
      !valid_git_identity(result.source_tree) ||
      !valid_sha256(result.executable_sha256) ||
      result.executable_build_id.empty()) {
    throw ReplayError(CP2OfflineReplayStatus::kInvalidArtifact,
                      "pre-seal replay identity is invalid");
  }
  static const std::vector<std::string> resolved_keys = {
      "sequence_index", "canonical_path", "canonical_sha256"};
  const std::vector<JsonValue> &records = as_array(
      required(root, "resolved_parameters"), "resolved parameters");
  if (records.empty()) {
    throw ReplayError(CP2OfflineReplayStatus::kInvalidArtifact,
                      "pre-seal resolved-parameter population is empty");
  }
  result.resolved_parameters.reserve(records.size());
  for (std::size_t index = 0U; index < records.size(); ++index) {
    const auto &record = as_object(records[index], "resolved parameter record");
    require_exact_keys(record, resolved_keys, "resolved parameter record");
    const std::uint64_t sequence =
        as_u64(required(record, "sequence_index"), "sequence index");
    const std::string path =
        as_string(required(record, "canonical_path"), "canonical path");
    const std::string sha256 =
        as_string(required(record, "canonical_sha256"), "canonical SHA-256");
    if (sequence != index || !safe_relative_path(path) ||
        !valid_sha256(sha256)) {
      throw ReplayError(CP2OfflineReplayStatus::kInvalidArtifact,
                        "pre-seal resolved-parameter join is invalid");
    }
    const FileBytes canonical =
        read_artifact_file(artifact_directory, path, kMaximumJsonBytes);
    if (canonical.sha256 != sha256) {
      throw ReplayError(CP2OfflineReplayStatus::kInvalidArtifact,
                        "pre-seal resolved-parameter hash mismatch");
    }
    result.resolved_parameters.push_back(
        ParameterReader(canonical.bytes).Parse(sequence, sha256));
  }
  return result;
}

const std::vector<std::string> &serial_pair_keys() {
  static const std::vector<std::string> keys = {
      "schema_version", "record_type", "sequence_index", "sequence_id",
      "pair_index", "anchor_filtered_index", "anchor_camera_id",
      "cam0_filtered_index", "cam1_filtered_index", "cam0_record_time_ns",
      "cam1_record_time_ns", "cam0_header_time_ns", "cam1_header_time_ns",
      "camera_timestamp_ns", "absolute_record_delta_ns", "selected",
      "enqueue_entered", "enqueue_returned", "enqueue_status",
      "processing_entered", "processing_returned", "processing_status",
      "updater_invocation_ids"};
  return keys;
}

const std::vector<std::string> &update_keys() {
  static const std::vector<std::string> keys = {
      "schema_version", "record_type", "sequence_index", "sequence_id",
      "pair_index", "camera_timestamp_ns", "invocation_id", "live_mode",
      "shadow_mode", "shadow_enabled", "timing_evidence_eligible",
      "duration_ns", "terminal_status", "terminal_subreason",
      "input_feature_count", "raw_system_count", "prior_snapshot_sha256",
      "precommit_snapshot_sha256", "prior_payload_offset",
      "prior_payload_length", "precommit_payload_offset",
      "precommit_payload_length", "expected_postcommit_snapshot_sha256",
      "expected_postcommit_payload_offset",
      "expected_postcommit_payload_length", "live_postcommit_snapshot_sha256",
      "live_postcommit_payload_offset", "live_postcommit_payload_length",
      "baseline_proposal_sha256", "baseline_proposal_payload_offset",
      "baseline_proposal_payload_length", "candidate_proposal_sha256",
      "candidate_proposal_payload_offset", "candidate_proposal_payload_length",
      "zero_write_snapshot_equal", "baseline_accepted_ids",
      "baseline_accepted_set_sha256", "baseline_accepted_sequence_sha256",
      "candidate_accepted_ids", "candidate_accepted_set_sha256",
      "candidate_accepted_sequence_sha256", "baseline_gamma_status",
      "baseline_gamma", "candidate_gamma", "baseline_precompression_rows",
      "baseline_compressed_rows", "candidate_precompression_rows",
      "candidate_compressed_rows", "baseline_preview_status",
      "baseline_preview_stage", "candidate_outcome",
      "candidate_preview_status", "candidate_preview_stage",
      "candidate_proposal_available", "baseline_preview_counters",
      "candidate_preview_counters", "baseline_commit_count",
      "baseline_transaction_mean_commits", "baseline_covariance_commits",
      "baseline_expected_type_update_calls",
      "baseline_verified_nominal_fields", "baseline_nominal_mismatches",
      "baseline_covariance_mismatches", "baseline_fej_mismatches",
      "candidate_ekf_update_calls", "candidate_type_update_calls",
      "candidate_mean_writes", "candidate_covariance_writes",
      "candidate_feature_writes", "state_block_rows",
      "covariance_block_rows", "all_block_rows_present", "math_passed",
      "config_sha256", "bag_sha256", "pair_index_sha256",
      "resolved_parameters_sha256"};
  return keys;
}

const std::vector<std::string> &feature_keys() {
  static const std::vector<std::string> keys = {
      "schema_version", "record_type", "sequence_index", "pair_index",
      "camera_timestamp_ns", "invocation_id", "feature_ordinal",
      "feature_id", "pass_index", "raw_rows", "raw_system_sha256",
      "jacobian_layout", "raw_payload_offset", "raw_payload_length",
      "prior_snapshot_sha256", "config_sha256", "bag_sha256",
      "pair_index_sha256", "resolved_parameters_sha256",
      "baseline_accepted_set_sha256", "baseline_accepted_sequence_sha256",
      "candidate_accepted_set_sha256", "candidate_accepted_sequence_sha256",
      "baseline_retained_gamma", "candidate_retained_gamma",
      "nullspace_reduction_status", "nullspace_reduction_stage",
      "nullspace_reduced_rows", "nullspace_raw_lambda_symmetry_error_inf",
      "nullspace_gamma", "nullspace_nis", "nullspace_threshold",
      "nullspace_gate_stage", "nullspace_decision", "schur_reduction_status",
      "schur_reduction_stage", "schur_reduced_rows",
      "schur_singular_values_available", "schur_singular_values",
      "schur_ratio_available", "schur_ratio",
      "schur_raw_lambda_symmetry_error_inf", "schur_gamma", "schur_nis",
      "schur_threshold", "schur_gate_stage", "schur_decision",
      "statistics_comparison_required", "lambda_comparison_available",
      "eta_comparison_available", "gamma_comparison_available",
      "lambda_comparison_status", "eta_comparison_status",
      "gamma_comparison_status", "lambda_reference_norm", "lambda_error",
      "lambda_tolerance", "lambda_ratio", "lambda_pass",
      "eta_reference_norm", "eta_error", "eta_tolerance", "eta_ratio",
      "eta_pass", "gamma_reference_norm", "gamma_error",
      "gamma_tolerance", "gamma_ratio", "gamma_pass", "agreement_class",
      "raw_row_match_weight", "nullspace_reducer_counters",
      "schur_reducer_counters"};
  return keys;
}

const std::vector<std::string> &state_block_keys() {
  static const std::vector<std::string> keys = {
      "schema_version", "record_type", "sequence_index", "pair_index",
      "camera_timestamp_ns", "invocation_id", "block_index", "block_kind",
      "block_identity", "covariance_id", "size", "candidate_available",
      "comparison_available", "comparison_status", "reference_norm", "error",
      "tolerance", "ratio", "prior_snapshot_sha256", "config_sha256",
      "bag_sha256", "pair_index_sha256", "resolved_parameters_sha256",
      "baseline_accepted_set_sha256", "baseline_accepted_sequence_sha256",
      "candidate_accepted_set_sha256", "candidate_accepted_sequence_sha256",
      "baseline_retained_gamma", "candidate_retained_gamma", "passed"};
  return keys;
}

const std::vector<std::string> &covariance_block_keys() {
  static const std::vector<std::string> keys = {
      "schema_version", "record_type", "sequence_index", "pair_index",
      "camera_timestamp_ns", "invocation_id", "row_block_index",
      "column_block_index", "row_covariance_id", "column_covariance_id",
      "row_size", "column_size", "candidate_available",
      "comparison_available", "comparison_status", "reference_norm", "error",
      "tolerance", "ratio", "prior_snapshot_sha256", "config_sha256",
      "bag_sha256", "pair_index_sha256", "resolved_parameters_sha256",
      "baseline_accepted_set_sha256", "baseline_accepted_sequence_sha256",
      "candidate_accepted_set_sha256", "candidate_accepted_sequence_sha256",
      "baseline_retained_gamma", "candidate_retained_gamma", "passed"};
  return keys;
}

std::uint64_t eigen_u64(Eigen::Index value, const char *what) {
  std::uint64_t converted = 0U;
  if (!ov_msckf::cp2_checked_eigen_index_to_u64(value, converted)) {
    throw ReplayError(CP2OfflineReplayStatus::kReplayFailed,
                      std::string(what) + " does not fit u64");
  }
  return converted;
}

ov_msckf::CP2FeatureGate::ChiSquaredTable chi_squared_table() {
  ov_msckf::CP2FeatureGate::ChiSquaredTable result;
  for (int degrees_of_freedom = 1; degrees_of_freedom < 500;
       ++degrees_of_freedom) {
    boost::math::chi_squared distribution(degrees_of_freedom);
    const double value = boost::math::quantile(distribution, 0.95);
    if (!std::isfinite(value) || !(value > 0.0) ||
        !result.emplace(degrees_of_freedom, value).second) {
      throw ReplayError(CP2OfflineReplayStatus::kReplayFailed,
                        "chi-squared startup table could not be reconstructed");
    }
  }
  return result;
}

bool payload_reference_equal(const std::map<std::string, JsonValue> &row,
                             const std::string &sha_key,
                             const std::string &offset_key,
                             const std::string &length_key,
                             bool available,
                             const ov_msckf::CP2TracePayloadReference &reference) {
  const JsonValue &sha = required(row, sha_key);
  const JsonValue &offset = required(row, offset_key);
  const JsonValue &length = required(row, length_key);
  if (!available) {
    return nullable(sha) && nullable(offset) && nullable(length);
  }
  return !nullable(sha) && !nullable(offset) && !nullable(length) &&
         as_string(sha, sha_key.c_str()) == reference.sha256 &&
         as_u64(offset, offset_key.c_str()) == reference.offset &&
         as_u64(length, length_key.c_str()) == reference.length;
}

bool identity_fields_equal(const std::map<std::string, JsonValue> &row,
                           const Identity &identity) {
  return identity_from_row(row) == identity;
}

struct UpdateView {
  Identity identity;
  const std::map<std::string, JsonValue> *row = nullptr;
  std::uint64_t raw_system_count = 0U;
  bool committed = false;
};

bool counter_object_equal(const JsonValue &value, std::uint64_t jitter,
                          std::uint64_t repair,
                          std::uint64_t alternate_solve,
                          std::uint64_t clamp,
                          std::uint64_t regularization,
                          std::uint64_t silent_fallback,
                          std::uint64_t fallback) {
  const auto &object = as_object(value, "counter object");
  static const std::vector<std::string> keys = {
      "jitter", "repair", "alternate_solve", "clamp", "regularization",
      "silent_fallback", "fallback"};
  require_exact_keys(object, keys, "counter object");
  return as_u64(required(object, "jitter"), "jitter") == jitter &&
         as_u64(required(object, "repair"), "repair") == repair &&
         as_u64(required(object, "alternate_solve"), "alternate_solve") ==
             alternate_solve &&
         as_u64(required(object, "clamp"), "clamp") == clamp &&
         as_u64(required(object, "regularization"), "regularization") ==
             regularization &&
         as_u64(required(object, "silent_fallback"), "silent_fallback") ==
             silent_fallback &&
         as_u64(required(object, "fallback"), "fallback") == fallback;
}

bool preview_counters_equal(
    const JsonValue &value,
    const ov_msckf::MSCKFUpdatePreviewDiagnostics &diagnostics) {
  return counter_object_equal(
      value, diagnostics.jitter_count, diagnostics.repair_count,
      diagnostics.alternate_solve_count, diagnostics.clamp_count,
      diagnostics.regularization_count, 0U, diagnostics.fallback_count);
}

bool statistic_equal(const std::map<std::string, JsonValue> &row,
                     const char *prefix,
                     const ov_msckf::CP2StatisticComparison &comparison) {
  const std::string stem(prefix);
  return as_bool(required(row, stem + "_comparison_available"),
                 "comparison availability") == comparison.available &&
         as_string(required(row, stem + "_comparison_status"),
                   "comparison status") ==
             ov_msckf::cp2_statistic_comparison_status_name(comparison.status) &&
         nullable_double_equal(required(row, stem + "_reference_norm"),
                               comparison.available,
                               comparison.reference_norm) &&
         nullable_double_equal(required(row, stem + "_error"),
                               comparison.available, comparison.error) &&
         nullable_double_equal(required(row, stem + "_tolerance"),
                               comparison.available, comparison.tolerance) &&
         nullable_double_equal(required(row, stem + "_ratio"),
                               comparison.available, comparison.ratio) &&
         as_bool(required(row, stem + "_pass"), "comparison pass") ==
             comparison.passed;
}

bool layout_equal(const JsonValue &value,
                  const std::vector<ov_msckf::CP2FeatureGateLayoutBlock> &layout) {
  const std::vector<JsonValue> &rows = as_array(value, "jacobian_layout");
  if (rows.size() != layout.size()) {
    return false;
  }
  static const std::vector<std::string> keys = {"covariance_id", "size"};
  for (std::size_t index = 0U; index < rows.size(); ++index) {
    const auto &row = as_object(rows[index], "jacobian layout block");
    require_exact_keys(row, keys, "jacobian layout block");
    if (as_u64(required(row, "covariance_id"), "covariance_id") !=
            eigen_u64(layout[index].covariance_id, "layout covariance ID") ||
        as_u64(required(row, "size"), "layout size") !=
            eigen_u64(layout[index].size, "layout size")) {
      return false;
    }
  }
  return true;
}

bool singular_values_equal(const JsonValue &value, bool available,
                           const Eigen::Vector3d &expected) {
  if (!available) {
    return nullable(value);
  }
  if (nullable(value)) {
    return false;
  }
  const std::vector<JsonValue> &values = as_array(value, "singular values");
  return values.size() == 3U &&
         binary64_equal(as_double(values[0], "singular value"), expected[0]) &&
         binary64_equal(as_double(values[1], "singular value"), expected[1]) &&
         binary64_equal(as_double(values[2], "singular value"), expected[2]);
}

void compare_feature_row(
    const std::map<std::string, JsonValue> &row, const Identity &identity,
    const ov_msckf::CP2RawSystemTraceFrame &raw,
    const ov_msckf::CP2FeaturePairResult &feature,
    const ov_msckf::CP2AcceptedFeatureDigests &baseline,
    const ov_msckf::CP2AcceptedFeatureDigests &candidate,
    const ov_msckf::CP2GlobalModeResult &baseline_global,
    const ov_msckf::CP2GlobalModeResult &candidate_global,
    const ov_msckf::CP2TracePayloadReference &prior_reference,
    FailureCounts &failures) {
  bool layout_mismatch = !identity_fields_equal(row, identity) ||
                         as_u64(required(row, "feature_ordinal"),
                                "feature_ordinal") != raw.feature_ordinal ||
                         as_u64(required(row, "feature_id"), "feature_id") !=
                             raw.raw_system.feature_id ||
                         as_u64(required(row, "pass_index"), "pass_index") != 1U ||
                         as_u64(required(row, "raw_rows"), "raw_rows") !=
                             eigen_u64(raw.raw_system.H_x.rows(), "raw rows") ||
                         as_string(required(row, "raw_system_sha256"),
                                   "raw_system_sha256") != raw.payload.sha256 ||
                         as_u64(required(row, "raw_payload_offset"),
                                "raw_payload_offset") != raw.payload.offset ||
                         as_u64(required(row, "raw_payload_length"),
                                "raw_payload_length") != raw.payload.length ||
                         as_string(required(row, "prior_snapshot_sha256"),
                                   "prior_snapshot_sha256") !=
                             prior_reference.sha256 ||
                         !layout_equal(required(row, "jacobian_layout"),
                                       raw.raw_system.jacobian_layout);
  if (layout_mismatch) {
    increment(failures.layout, "layout failure");
  }

  bool accepted_mismatch =
      as_string(required(row, "baseline_accepted_set_sha256"),
                "baseline accepted set") != baseline.set_sha256 ||
      as_string(required(row, "baseline_accepted_sequence_sha256"),
                "baseline accepted sequence") != baseline.sequence_sha256 ||
      as_string(required(row, "candidate_accepted_set_sha256"),
                "candidate accepted set") != candidate.set_sha256 ||
      as_string(required(row, "candidate_accepted_sequence_sha256"),
                "candidate accepted sequence") != candidate.sequence_sha256;
  if (accepted_mismatch) {
    increment(failures.accepted_sequence, "accepted-sequence failure");
  }

  const bool baseline_gamma_available =
      baseline_global.gamma_status == ov_msckf::CP2GammaStatus::kAvailable;
  const bool candidate_gamma_available =
      candidate_global.gamma_status == ov_msckf::CP2GammaStatus::kAvailable;
  if (!nullable_double_equal(required(row, "baseline_retained_gamma"),
                             baseline_gamma_available,
                             baseline_global.retained_gamma) ||
      !nullable_double_equal(required(row, "candidate_retained_gamma"),
                             candidate_gamma_available,
                             candidate_global.retained_gamma)) {
    increment(failures.gamma, "gamma failure");
  }

  bool reduction_mismatch =
      as_string(required(row, "nullspace_reduction_status"),
                "nullspace status") !=
          ov_msckf::cp2_nullspace_reduction_status_name(feature.nullspace.status) ||
      as_string(required(row, "nullspace_reduction_stage"),
                "nullspace stage") !=
          ov_msckf::cp2_nullspace_reduction_stage_name(feature.nullspace.stage) ||
      as_u64(required(row, "nullspace_reduced_rows"),
             "nullspace reduced rows") !=
          eigen_u64(feature.nullspace.reduced_rows, "nullspace reduced rows") ||
      !nullable_double_equal(
          required(row, "nullspace_raw_lambda_symmetry_error_inf"),
          feature.nullspace.statistics.raw_lambda_symmetry_error_available,
          feature.nullspace.statistics.raw_lambda_symmetry_error_inf) ||
      !nullable_double_equal(required(row, "nullspace_gamma"),
                             feature.nullspace.mode_gamma_available,
                             feature.nullspace.mode_gamma) ||
      as_string(required(row, "schur_reduction_status"), "schur status") !=
          ov_msckf::schur_reduction_status_name(feature.schur.status) ||
      as_string(required(row, "schur_reduction_stage"), "schur stage") !=
          ov_msckf::schur_reduction_stage_name(feature.schur.stage) ||
      as_u64(required(row, "schur_reduced_rows"), "schur reduced rows") !=
          eigen_u64(feature.schur.H_reduced.rows(), "schur reduced rows") ||
      as_bool(required(row, "schur_singular_values_available"),
              "schur singular values available") !=
          feature.schur.singular_values_available ||
      !singular_values_equal(required(row, "schur_singular_values"),
                             feature.schur.singular_values_available,
                             feature.schur.singular_values) ||
      as_bool(required(row, "schur_ratio_available"),
              "schur ratio available") != feature.schur.singular_ratio_available ||
      !nullable_double_equal(required(row, "schur_ratio"),
                             feature.schur.singular_ratio_available,
                             feature.schur.singular_ratio) ||
      !nullable_double_equal(
          required(row, "schur_raw_lambda_symmetry_error_inf"),
          feature.schur.accepted(),
          feature.schur.raw_lambda_symmetry_error_inf) ||
      !nullable_double_equal(required(row, "schur_gamma"),
                             feature.schur.accepted(), feature.schur.gamma) ||
      !counter_object_equal(
          required(row, "nullspace_reducer_counters"),
          feature.nullspace.counters.jitter_count,
          feature.nullspace.counters.repair_count,
          feature.nullspace.counters.alternate_solve_count,
          feature.nullspace.counters.clamp_count,
          feature.nullspace.counters.regularization_count,
          feature.nullspace.counters.silent_fallback_count,
          feature.nullspace.counters.fallback_count) ||
      !counter_object_equal(required(row, "schur_reducer_counters"),
                            feature.schur.jitter_count, 0U, 0U,
                            feature.schur.clamp_count,
                            feature.schur.regularization_count, 0U,
                            feature.schur.fallback_count);
  if (reduction_mismatch) {
    increment(failures.reduction, "reduction failure");
  }

  bool gate_mismatch =
      as_string(required(row, "nullspace_gate_stage"), "nullspace gate stage") !=
          ov_msckf::cp2_feature_gate_stage_name(feature.nullspace_gate.stage) ||
      !nullable_double_equal(required(row, "nullspace_nis"),
                             feature.nullspace_gate.chi2_available,
                             feature.nullspace_gate.chi2) ||
      !nullable_double_equal(required(row, "nullspace_threshold"),
                             feature.nullspace_gate.threshold_available,
                             feature.nullspace_gate.threshold) ||
      !nullable_bool_equal(required(row, "nullspace_decision"),
                           feature.nullspace_gate.evidence_decision_available,
                           feature.nullspace_gate.evidence_accept) ||
      as_string(required(row, "schur_gate_stage"), "schur gate stage") !=
          ov_msckf::cp2_feature_gate_stage_name(feature.schur_gate.stage) ||
      !nullable_double_equal(required(row, "schur_nis"),
                             feature.schur_gate.chi2_available,
                             feature.schur_gate.chi2) ||
      !nullable_double_equal(required(row, "schur_threshold"),
                             feature.schur_gate.threshold_available,
                             feature.schur_gate.threshold) ||
      !nullable_bool_equal(required(row, "schur_decision"),
                           feature.schur_gate.evidence_decision_available,
                           feature.schur_gate.evidence_accept) ||
      as_string(required(row, "agreement_class"), "agreement class") !=
          ov_msckf::cp2_gate_agreement_class_name(feature.agreement_class) ||
      as_u64(required(row, "raw_row_match_weight"), "raw row match weight") !=
          feature.raw_row_match_weight;
  if (gate_mismatch) {
    increment(failures.gate, "gate failure");
  }

  bool statistics_mismatch =
      as_bool(required(row, "statistics_comparison_required"),
              "statistics required") != feature.statistics_comparison_required ||
      !statistic_equal(row, "lambda", feature.lambda_comparison) ||
      !statistic_equal(row, "eta", feature.eta_comparison) ||
      !statistic_equal(row, "gamma", feature.gamma_comparison);
  if (statistics_mismatch) {
    increment(failures.statistics, "statistics failure");
  }
}

std::string candidate_outcome(const ov_msckf::CP2ShadowMathResult &math) {
  if (!math.schur_assembly_valid) {
    return "internal_failure";
  }
  if (math.schur.gamma_status == ov_msckf::CP2GammaStatus::kNonfinite) {
    return "gamma_nonfinite";
  }
  if (math.schur.accepted_ids.empty() || math.schur.precompression_rows < 1) {
    return "all_rejected";
  }
  if (math.schur.compressed_rows < 1) {
    return "empty_after_compression";
  }
  return math.schur.proposal_available ? "proposal_available"
                                       : "preflight_rejected";
}

std::pair<std::string, std::string>
baseline_terminal(const ov_msckf::CP2ShadowMathResult &math) {
  if (!math.nullspace_assembly_valid) {
    return {"internal_failure", "trace_invariant_failure"};
  }
  if (math.nullspace.accepted_ids.empty() ||
      math.nullspace.precompression_rows < 1) {
    return {"all_rejected", "all_baseline_features_rejected"};
  }
  if (math.nullspace.compressed_rows < 1) {
    return {"empty_after_compression", "measurement_compression_empty"};
  }
  if (!math.nullspace.proposal_available) {
    return {"preflight_rejected", "baseline_preflight_rejected"};
  }
  return {"committed_counted", "none"};
}

void compare_update_row(
    const std::map<std::string, JsonValue> &row,
    const ov_msckf::CP2TraceReplayResult &replay,
    const std::vector<ov_msckf::CP2StateTraceFrame> &states,
    const std::vector<ov_msckf::CP2ProposalTraceFrame> &proposals,
    FailureCounts &failures) {
  const ov_msckf::CP2ShadowMathResult &math = replay.math;
  if (as_u64_array(required(row, "baseline_accepted_ids"),
                   "baseline accepted IDs") !=
          replay.baseline_accepted.processing_sequence ||
      as_string(required(row, "baseline_accepted_set_sha256"),
                "baseline set digest") != replay.baseline_accepted.set_sha256 ||
      as_string(required(row, "baseline_accepted_sequence_sha256"),
                "baseline sequence digest") !=
          replay.baseline_accepted.sequence_sha256 ||
      as_u64_array(required(row, "candidate_accepted_ids"),
                   "candidate accepted IDs") !=
          replay.candidate_accepted.processing_sequence ||
      as_string(required(row, "candidate_accepted_set_sha256"),
                "candidate set digest") != replay.candidate_accepted.set_sha256 ||
      as_string(required(row, "candidate_accepted_sequence_sha256"),
                "candidate sequence digest") !=
          replay.candidate_accepted.sequence_sha256) {
    increment(failures.accepted_sequence, "accepted-sequence failure");
  }

  const bool baseline_gamma_available =
      math.nullspace.gamma_status == ov_msckf::CP2GammaStatus::kAvailable;
  const bool candidate_gamma_available =
      math.schur.gamma_status == ov_msckf::CP2GammaStatus::kAvailable;
  if (as_string(required(row, "baseline_gamma_status"),
                "baseline gamma status") !=
          ov_msckf::cp2_gamma_status_name(math.nullspace.gamma_status) ||
      !nullable_double_equal(required(row, "baseline_gamma"),
                             baseline_gamma_available,
                             math.nullspace.retained_gamma) ||
      !nullable_double_equal(required(row, "candidate_gamma"),
                             candidate_gamma_available,
                             math.schur.retained_gamma)) {
    increment(failures.gamma, "gamma failure");
  }

  const bool baseline_compression_reached =
      math.nullspace.precompression_rows > 0;
  const bool candidate_compression_reached =
      math.schur.precompression_rows > 0 && candidate_gamma_available;
  if (!nullable_u64_equal(required(row, "baseline_precompression_rows"), true,
                          eigen_u64(math.nullspace.precompression_rows,
                                    "baseline precompression rows")) ||
      !nullable_u64_equal(
          required(row, "candidate_precompression_rows"), true,
          eigen_u64(math.schur.precompression_rows,
                    "candidate precompression rows"))) {
    increment(failures.stack, "stack failure");
  }
  if (!nullable_u64_equal(
          required(row, "baseline_compressed_rows"),
          baseline_compression_reached,
          eigen_u64(math.nullspace.compressed_rows,
                    "baseline compressed rows")) ||
      !nullable_u64_equal(
          required(row, "candidate_compressed_rows"),
          candidate_compression_reached,
          eigen_u64(math.schur.compressed_rows, "candidate compressed rows"))) {
    increment(failures.compression, "compression failure");
  }

  const bool baseline_preview_reached =
      baseline_compression_reached && math.nullspace.compressed_rows > 0;
  const bool candidate_preview_reached =
      candidate_compression_reached && math.schur.compressed_rows > 0;
  const std::pair<std::string, std::string> terminal = baseline_terminal(math);
  if (as_string(required(row, "terminal_status"), "terminal status") !=
          terminal.first ||
      as_string(required(row, "terminal_subreason"), "terminal subreason") !=
          terminal.second ||
      !nullable_string_equal(
          required(row, "baseline_preview_status"), baseline_preview_reached,
          ov_msckf::msckf_update_preview_status_name(
              math.nullspace.proposal.diagnostics.status)) ||
      !nullable_string_equal(
          required(row, "baseline_preview_stage"), baseline_preview_reached,
          ov_msckf::msckf_update_preview_stage_name(
              math.nullspace.proposal.diagnostics.stage)) ||
      as_string(required(row, "candidate_outcome"), "candidate outcome") !=
          candidate_outcome(math) ||
      !nullable_string_equal(
          required(row, "candidate_preview_status"), candidate_preview_reached,
          ov_msckf::msckf_update_preview_status_name(
              math.schur.proposal.diagnostics.status)) ||
      !nullable_string_equal(
          required(row, "candidate_preview_stage"), candidate_preview_reached,
          ov_msckf::msckf_update_preview_stage_name(
              math.schur.proposal.diagnostics.stage)) ||
      !preview_counters_equal(required(row, "baseline_preview_counters"),
                              math.nullspace.proposal.diagnostics) ||
      !preview_counters_equal(required(row, "candidate_preview_counters"),
                              math.schur.proposal.diagnostics)) {
    increment(failures.preview_status, "preview-status failure");
  }

  const auto find_role = [&proposals](ov_msckf::CP2ProposalTraceRole role)
      -> const ov_msckf::CP2ProposalTraceFrame * {
    for (const ov_msckf::CP2ProposalTraceFrame &proposal : proposals) {
      if (proposal.role == role) {
        return &proposal;
      }
    }
    return nullptr;
  };
  const ov_msckf::CP2ProposalTraceFrame *baseline_proposal =
      find_role(ov_msckf::CP2ProposalTraceRole::kNullspaceBaseline);
  const ov_msckf::CP2ProposalTraceFrame *candidate_proposal =
      find_role(ov_msckf::CP2ProposalTraceRole::kSchurCandidate);
  const ov_msckf::CP2TracePayloadReference empty_reference;
  if (as_bool(required(row, "candidate_proposal_available"),
              "candidate proposal available") != math.schur.proposal_available ||
      !payload_reference_equal(
          row, "baseline_proposal_sha256", "baseline_proposal_payload_offset",
          "baseline_proposal_payload_length", math.nullspace.proposal_available,
          baseline_proposal == nullptr ? empty_reference
                                       : baseline_proposal->payload) ||
      !payload_reference_equal(
          row, "candidate_proposal_sha256", "candidate_proposal_payload_offset",
          "candidate_proposal_payload_length", math.schur.proposal_available,
          candidate_proposal == nullptr ? empty_reference
                                        : candidate_proposal->payload)) {
    increment(failures.proposal_presence, "proposal-presence failure");
  }

  if (states.size() != 2U && states.size() != 4U) {
    increment(failures.commit_oracle, "commit-oracle failure");
    return;
  }
  if (!payload_reference_equal(row, "prior_snapshot_sha256",
                               "prior_payload_offset", "prior_payload_length",
                               true, states[0].payload) ||
      !payload_reference_equal(row, "precommit_snapshot_sha256",
                               "precommit_payload_offset",
                               "precommit_payload_length", true,
                               states[1].payload) ||
      !ov_msckf::CP2CompositeStateAdapter::CanonicallyEqual(
          states[0].snapshot, states[1].snapshot) ||
      !as_bool(required(row, "zero_write_snapshot_equal"),
               "zero write snapshot equality")) {
    increment(failures.commit_oracle, "commit-oracle failure");
  }
}

void compare_detached_commit(
    const std::map<std::string, JsonValue> &row,
    const ov_msckf::CP2TraceReplayResult &replay,
    const std::vector<ov_msckf::CP2StateTraceFrame> &states,
    FailureCounts &failures) {
  const bool expected_commit = replay.math.nullspace.proposal_available;
  const std::uint64_t expected_one = expected_commit ? 1U : 0U;
  const std::uint64_t expected_blocks =
      expected_commit ? static_cast<std::uint64_t>(states[0].snapshot.semantic_blocks.size())
                      : 0U;
  std::uint64_t expected_covariance_blocks = 0U;
  if (!ov_msckf::cp2_checked_multiply_u64(expected_blocks, expected_blocks,
                                          expected_covariance_blocks)) {
    throw ReplayError(CP2OfflineReplayStatus::kReplayFailed,
                      "block population overflows u64");
  }

  bool mismatch =
      as_u64(required(row, "baseline_commit_count"), "baseline commit count") !=
          expected_one ||
      as_u64(required(row, "baseline_transaction_mean_commits"),
             "baseline mean commits") != expected_one ||
      as_u64(required(row, "baseline_covariance_commits"),
             "baseline covariance commits") != expected_one ||
      as_u64(required(row, "candidate_ekf_update_calls"),
             "candidate EKF calls") != 0U ||
      as_u64(required(row, "candidate_type_update_calls"),
             "candidate Type calls") != 0U ||
      as_u64(required(row, "candidate_mean_writes"),
             "candidate mean writes") != 0U ||
      as_u64(required(row, "candidate_covariance_writes"),
             "candidate covariance writes") != 0U ||
      as_u64(required(row, "candidate_feature_writes"),
             "candidate feature writes") != 0U ||
      as_u64(required(row, "state_block_rows"), "state block rows") !=
          expected_blocks ||
      as_u64(required(row, "covariance_block_rows"),
             "covariance block rows") != expected_covariance_blocks ||
      !as_bool(required(row, "all_block_rows_present"),
               "all block rows present");

  const ov_msckf::CP2TracePayloadReference empty_reference;
  if (!expected_commit) {
    mismatch = mismatch || states.size() != 2U ||
               !payload_reference_equal(
                   row, "expected_postcommit_snapshot_sha256",
                   "expected_postcommit_payload_offset",
                   "expected_postcommit_payload_length", false, empty_reference) ||
               !payload_reference_equal(
                   row, "live_postcommit_snapshot_sha256",
                   "live_postcommit_payload_offset",
                   "live_postcommit_payload_length", false, empty_reference) ||
               as_u64(required(row, "baseline_expected_type_update_calls"),
                      "expected Type calls") != 0U ||
               as_u64(required(row, "baseline_verified_nominal_fields"),
                      "verified nominal fields") != 0U ||
               as_u64(required(row, "baseline_nominal_mismatches"),
                      "nominal mismatches") != 0U ||
               as_u64(required(row, "baseline_covariance_mismatches"),
                      "covariance mismatches") != 0U ||
               as_u64(required(row, "baseline_fej_mismatches"),
                      "FEJ mismatches") != 0U;
    if (mismatch) {
      increment(failures.commit_oracle, "commit-oracle failure");
    }
    return;
  }

  if (states.size() != 4U) {
    increment(failures.commit_oracle, "commit-oracle failure");
    return;
  }
  ov_msckf::CP2CompositeStateSnapshot derived;
  std::uint64_t type_update_calls = 0U;
  const ov_msckf::CP2CompositeStateStatus build_status =
      ov_msckf::CP2CompositeStateAdapter::BuildExpected(
          states[0].snapshot, replay.math.nullspace.proposal, derived,
          type_update_calls);
  if (build_status != ov_msckf::CP2CompositeStateStatus::kAccepted) {
    increment(failures.commit_oracle, "commit-oracle failure");
    return;
  }
  const std::vector<std::uint8_t> derived_bytes =
      ov_msckf::CP2StateTraceCodec::EncodeSnapshotPayload(derived);
  const std::vector<std::uint8_t> phase2_bytes =
      ov_msckf::CP2StateTraceCodec::EncodeSnapshotPayload(states[2].snapshot);
  const std::vector<std::uint8_t> phase3_bytes =
      ov_msckf::CP2StateTraceCodec::EncodeSnapshotPayload(states[3].snapshot);
  if (derived_bytes != phase2_bytes || derived_bytes != phase3_bytes) {
    mismatch = true;
  }
  mismatch = mismatch ||
             !payload_reference_equal(
                 row, "expected_postcommit_snapshot_sha256",
                 "expected_postcommit_payload_offset",
                 "expected_postcommit_payload_length", true,
                 states[2].payload) ||
             !payload_reference_equal(
                 row, "live_postcommit_snapshot_sha256",
                 "live_postcommit_payload_offset",
                 "live_postcommit_payload_length", true, states[3].payload);

  const ov_msckf::CP2CommitOracleResult oracle =
      ov_msckf::CP2CommitOracle::Compare(states[0].snapshot, derived,
                                         states[3].snapshot,
                                         type_update_calls);
  mismatch = mismatch || !oracle.passed ||
             as_u64(required(row, "baseline_expected_type_update_calls"),
                    "expected Type calls") !=
                 oracle.baseline_expected_type_update_calls ||
             as_u64(required(row, "baseline_verified_nominal_fields"),
                    "verified nominal fields") !=
                 oracle.baseline_verified_nominal_fields ||
             as_u64(required(row, "baseline_nominal_mismatches"),
                    "nominal mismatches") != oracle.baseline_nominal_mismatches ||
             as_u64(required(row, "baseline_covariance_mismatches"),
                    "covariance mismatches") !=
                 oracle.baseline_covariance_mismatches ||
             as_u64(required(row, "baseline_fej_mismatches"),
                    "FEJ mismatches") != oracle.baseline_fej_mismatches;
  if (mismatch) {
    increment(failures.commit_oracle, "commit-oracle failure");
  }
}

struct BlockComparison {
  bool candidate_available = false;
  bool comparison_available = false;
  std::string status = "candidate_unavailable";
  double reference_norm = 0.0;
  double error = 0.0;
  double tolerance = 0.0;
  double ratio = 0.0;
  bool passed = false;
};

template <typename DerivedReference, typename DerivedCandidate>
BlockComparison block_comparison(
    const Eigen::MatrixBase<DerivedReference> &reference,
    bool candidate_available,
    const Eigen::MatrixBase<DerivedCandidate> &candidate) {
  BlockComparison result;
  result.candidate_available = candidate_available;
  if (!candidate_available) {
    return result;
  }
  result.reference_norm = reference.norm();
  if (!std::isfinite(result.reference_norm) || result.reference_norm < 0.0) {
    result.status = "reference_norm_nonfinite";
    return result;
  }
  const typename DerivedReference::PlainObject difference = candidate - reference;
  if (!difference.allFinite()) {
    result.status = "error_nonfinite";
    return result;
  }
  result.error = difference.norm();
  if (!std::isfinite(result.error) || result.error < 0.0) {
    result.status = "error_nonfinite";
    return result;
  }
  const double scaled = 1.0e-6 * result.reference_norm;
  result.tolerance = 1.0e-8 + scaled;
  if (!std::isfinite(scaled) || !std::isfinite(result.tolerance) ||
      result.tolerance < 0.0) {
    result.status = "tolerance_nonfinite";
    return result;
  }
  if (std::fegetround() != FE_TONEAREST) {
    throw ReplayError(CP2OfflineReplayStatus::kReplayFailed,
                      "block comparison ratio requires FE_TONEAREST");
  }
  result.ratio = result.error / result.tolerance;
  if (!std::isfinite(result.ratio) || result.ratio < 0.0) {
    result.status = "ratio_nonfinite";
    return result;
  }
  result.status = "available";
  result.comparison_available = true;
  result.passed = result.error <= result.tolerance;
  return result;
}

bool block_diagnostics_equal(const std::map<std::string, JsonValue> &row,
                             const BlockComparison &comparison) {
  return as_bool(required(row, "candidate_available"),
                 "candidate available") == comparison.candidate_available &&
         as_bool(required(row, "comparison_available"),
                 "comparison available") == comparison.comparison_available &&
         as_string(required(row, "comparison_status"), "comparison status") ==
             comparison.status &&
         nullable_double_equal(required(row, "reference_norm"),
                               comparison.comparison_available,
                               comparison.reference_norm) &&
         nullable_double_equal(required(row, "error"),
                               comparison.comparison_available,
                               comparison.error) &&
         nullable_double_equal(required(row, "tolerance"),
                               comparison.comparison_available,
                               comparison.tolerance) &&
         nullable_double_equal(required(row, "ratio"),
                               comparison.comparison_available,
                               comparison.ratio) &&
         as_bool(required(row, "passed"), "block passed") == comparison.passed;
}

const char *semantic_kind_name(ov_msckf::CP2SemanticStateKind kind) {
  static const std::array<const char *, 8U> names = {{
      "imu.theta", "imu.position", "imu.velocity", "imu.gyro_bias",
      "imu.accel_bias", "clone.theta", "clone.position", "slam_landmark"}};
  const std::size_t index = static_cast<std::size_t>(kind);
  if (index >= names.size()) {
    throw ReplayError(CP2OfflineReplayStatus::kReplayFailed,
                      "semantic block kind is invalid");
  }
  return names[index];
}

const char *landmark_representation_name(
    ov_msckf::CP2LandmarkRepresentation representation) {
  static const std::array<const char *, 6U> names = {{
      "GLOBAL_3D", "GLOBAL_FULL_INVERSE_DEPTH", "ANCHORED_3D",
      "ANCHORED_FULL_INVERSE_DEPTH", "ANCHORED_MSCKF_INVERSE_DEPTH",
      "ANCHORED_INVERSE_DEPTH_SINGLE"}};
  const std::size_t index = static_cast<std::size_t>(representation);
  if (index >= names.size()) {
    throw ReplayError(CP2OfflineReplayStatus::kReplayFailed,
                      "landmark representation is invalid");
  }
  return names[index];
}

std::string binary64_hex(double value) {
  std::uint64_t bits = 0U;
  std::memcpy(&bits, &value, sizeof(bits));
  std::ostringstream output;
  output.imbue(std::locale::classic());
  output << std::hex << std::nouppercase << std::setfill('0') << std::setw(16)
         << bits;
  return output.str();
}

bool block_identity_equal(const JsonValue &value,
                          const ov_msckf::CP2SemanticStateBlock &block) {
  const auto &identity = as_object(value, "block identity");
  const std::string kind = semantic_kind_name(block.kind);
  if (block.kind == ov_msckf::CP2SemanticStateKind::kCloneTheta ||
      block.kind == ov_msckf::CP2SemanticStateKind::kClonePosition) {
    static const std::vector<std::string> keys = {"kind", "timestamp_bits"};
    require_exact_keys(identity, keys, "clone block identity");
    return as_string(required(identity, "kind"), "block kind") == kind &&
           as_string(required(identity, "timestamp_bits"), "timestamp bits") ==
               binary64_hex(block.clone_timestamp);
  }
  if (block.kind == ov_msckf::CP2SemanticStateKind::kSlamLandmark) {
    static const std::vector<std::string> keys = {
        "kind", "feature_id", "representation", "anchor_camera_id",
        "anchor_timestamp_bits"};
    require_exact_keys(identity, keys, "landmark block identity");
    const JsonValue &anchor = required(identity, "anchor_camera_id");
    bool anchor_equal = false;
    if (anchor.kind == JsonKind::kU64 && block.landmark.anchor_camera_id >= 0) {
      anchor_equal = anchor.unsigned_integer ==
                     static_cast<std::uint64_t>(block.landmark.anchor_camera_id);
    } else if (anchor.kind == JsonKind::kI64) {
      anchor_equal = anchor.signed_integer == block.landmark.anchor_camera_id;
    }
    return as_string(required(identity, "kind"), "block kind") == kind &&
           as_u64(required(identity, "feature_id"), "landmark feature ID") ==
               block.landmark.feature_id &&
           as_string(required(identity, "representation"),
                     "landmark representation") ==
               landmark_representation_name(block.landmark.representation) &&
           anchor_equal &&
           as_string(required(identity, "anchor_timestamp_bits"),
                     "anchor timestamp bits") ==
               binary64_hex(block.landmark.anchor_timestamp);
  }
  static const std::vector<std::string> keys = {"kind"};
  require_exact_keys(identity, keys, "IMU block identity");
  return as_string(required(identity, "kind"), "block kind") == kind;
}

bool repeated_update_fields_equal(
    const std::map<std::string, JsonValue> &row,
    const std::map<std::string, JsonValue> &update) {
  static const std::array<const char *, 9U> strings = {{
      "prior_snapshot_sha256", "config_sha256", "bag_sha256",
      "pair_index_sha256", "resolved_parameters_sha256",
      "baseline_accepted_set_sha256", "baseline_accepted_sequence_sha256",
      "candidate_accepted_set_sha256", "candidate_accepted_sequence_sha256"}};
  for (const char *field : strings) {
    if (as_string(required(row, field), field) !=
        as_string(required(update, field), field)) {
      return false;
    }
  }
  const JsonValue &baseline = required(update, "baseline_gamma");
  const JsonValue &candidate = required(update, "candidate_gamma");
  const bool baseline_available = !nullable(baseline);
  const bool candidate_available = !nullable(candidate);
  return nullable_double_equal(
             required(row, "baseline_retained_gamma"), baseline_available,
             baseline_available ? as_double(baseline, "baseline gamma") : 0.0) &&
         nullable_double_equal(
             required(row, "candidate_retained_gamma"), candidate_available,
             candidate_available ? as_double(candidate, "candidate gamma") : 0.0);
}

void compare_block_rows(
    const Identity &identity, const std::map<std::string, JsonValue> &update,
    const ov_msckf::CP2CompositeStateSnapshot &prior,
    const ov_msckf::CP2GlobalModeResult &baseline,
    const ov_msckf::CP2GlobalModeResult &candidate,
    const std::vector<const std::map<std::string, JsonValue> *> &state_rows,
    const std::vector<const std::map<std::string, JsonValue> *> &covariance_rows,
    FailureCounts &failures) {
  const std::size_t block_count = prior.semantic_blocks.size();
  std::uint64_t covariance_count = 0U;
  if (!ov_msckf::cp2_checked_multiply_u64(
          static_cast<std::uint64_t>(block_count),
          static_cast<std::uint64_t>(block_count), covariance_count) ||
      state_rows.size() != block_count ||
      covariance_rows.size() != covariance_count ||
      !baseline.proposal_available) {
    increment(failures.block_metrics, "block-metric failure");
    return;
  }

  const Eigen::VectorXd empty_vector;
  const Eigen::MatrixXd empty_matrix;
  for (std::size_t index = 0U; index < block_count; ++index) {
    const auto &row = *state_rows[index];
    const ov_msckf::CP2SemanticStateBlock &block = prior.semantic_blocks[index];
    if (block.covariance_id >
            static_cast<std::uint64_t>(std::numeric_limits<Eigen::Index>::max()) ||
        block.error_size >
            static_cast<std::uint64_t>(std::numeric_limits<Eigen::Index>::max())) {
      throw ReplayError(CP2OfflineReplayStatus::kReplayFailed,
                        "semantic state block exceeds Eigen index");
    }
    const Eigen::Index covariance_id =
        static_cast<Eigen::Index>(block.covariance_id);
    const Eigen::Index size = static_cast<Eigen::Index>(block.error_size);
    const auto reference = baseline.proposal.dx.segment(covariance_id, size);
    const auto candidate_segment =
        candidate.proposal_available
            ? candidate.proposal.dx.segment(covariance_id, size).eval()
            : empty_vector;
    const BlockComparison comparison = block_comparison(
        reference, candidate.proposal_available, candidate_segment);
    const bool mismatch =
        !identity_fields_equal(row, identity) ||
        as_u64(required(row, "block_index"), "block index") != index ||
        as_string(required(row, "block_kind"), "block kind") !=
            semantic_kind_name(block.kind) ||
        !block_identity_equal(required(row, "block_identity"), block) ||
        as_u64(required(row, "covariance_id"), "covariance ID") !=
            block.covariance_id ||
        as_u64(required(row, "size"), "block size") != block.error_size ||
        !repeated_update_fields_equal(row, update) ||
        !block_diagnostics_equal(row, comparison) ||
        !ov_msckf::cp2_offline_replay_internal::BlockComparisonPasses(
            comparison.comparison_available, comparison.passed);
    if (mismatch) {
      increment(failures.block_metrics, "block-metric failure");
    }
  }

  std::size_t ordinal = 0U;
  for (std::size_t row_index = 0U; row_index < block_count; ++row_index) {
    const ov_msckf::CP2SemanticStateBlock &row_block =
        prior.semantic_blocks[row_index];
    const Eigen::Index row_id =
        static_cast<Eigen::Index>(row_block.covariance_id);
    const Eigen::Index row_size =
        static_cast<Eigen::Index>(row_block.error_size);
    for (std::size_t column_index = 0U; column_index < block_count;
         ++column_index, ++ordinal) {
      const auto &row = *covariance_rows[ordinal];
      const ov_msckf::CP2SemanticStateBlock &column_block =
          prior.semantic_blocks[column_index];
      const Eigen::Index column_id =
          static_cast<Eigen::Index>(column_block.covariance_id);
      const Eigen::Index column_size =
          static_cast<Eigen::Index>(column_block.error_size);
      const auto reference = baseline.proposal.P_plus.block(
          row_id, column_id, row_size, column_size);
      const auto candidate_block =
          candidate.proposal_available
              ? candidate.proposal.P_plus
                    .block(row_id, column_id, row_size, column_size)
                    .eval()
              : empty_matrix;
      const BlockComparison comparison = block_comparison(
          reference, candidate.proposal_available, candidate_block);
      const bool mismatch =
          !identity_fields_equal(row, identity) ||
          as_u64(required(row, "row_block_index"), "row block index") !=
              row_index ||
          as_u64(required(row, "column_block_index"),
                 "column block index") != column_index ||
          as_u64(required(row, "row_covariance_id"), "row covariance ID") !=
              row_block.covariance_id ||
          as_u64(required(row, "column_covariance_id"),
                 "column covariance ID") != column_block.covariance_id ||
          as_u64(required(row, "row_size"), "row size") !=
              row_block.error_size ||
          as_u64(required(row, "column_size"), "column size") !=
              column_block.error_size ||
          !repeated_update_fields_equal(row, update) ||
          !block_diagnostics_equal(row, comparison) ||
          !ov_msckf::cp2_offline_replay_internal::BlockComparisonPasses(
              comparison.comparison_available, comparison.passed);
      if (mismatch) {
        increment(failures.block_metrics, "block-metric failure");
      }
    }
  }
}

using JsonRowPointer = const std::map<std::string, JsonValue> *;

std::map<TraceKey, std::uint64_t>
validate_serial_pairs(const std::vector<JsonValue> &rows) {
  std::map<TraceKey, std::uint64_t> invocations;
  std::map<std::uint64_t, std::uint64_t> next_pair;
  for (const JsonValue &value : rows) {
    const auto &row = as_object(value, "serial pair row");
    require_exact_keys(row, serial_pair_keys(), "serial pair row");
    require_schema_record(row, "serial_pair");
    const std::uint64_t sequence =
        as_u64(required(row, "sequence_index"), "sequence index");
    const std::uint64_t pair = as_u64(required(row, "pair_index"), "pair index");
    if (!ov_msckf::cp2_offline_replay_internal::
            FrozenSequenceIdentityPasses(
                sequence,
                as_string(required(row, "sequence_id"), "sequence ID")) ||
        pair != next_pair[sequence] ||
        as_u64(required(row, "camera_timestamp_ns"), "camera timestamp") !=
            as_u64(required(row, "cam0_header_time_ns"),
                   "cam0 header timestamp") ||
        as_u64(required(row, "absolute_record_delta_ns"),
               "absolute record delta") >= UINT64_C(20000000) ||
        !as_bool(required(row, "selected"), "selected")) {
      throw ReplayError(CP2OfflineReplayStatus::kInvalidArtifact,
                        "serial pair ordering/timestamp contract is invalid");
    }
    increment(next_pair[sequence], "pair index");
    const bool enqueue_entered =
        as_bool(required(row, "enqueue_entered"), "enqueue entered");
    const bool enqueue_returned =
        as_bool(required(row, "enqueue_returned"), "enqueue returned");
    const bool processing_entered =
        as_bool(required(row, "processing_entered"), "processing entered");
    const bool processing_returned =
        as_bool(required(row, "processing_returned"), "processing returned");
    const std::string enqueue =
        as_string(required(row, "enqueue_status"), "enqueue status");
    const std::string processing =
        as_string(required(row, "processing_status"), "processing status");
    const bool processed = enqueue == "queued" && processing == "processed" &&
                           enqueue_entered && enqueue_returned &&
                           processing_entered && processing_returned;
    const bool dropped = enqueue == "frequency_dropped" &&
                         processing == "not_queued" && enqueue_entered &&
                         enqueue_returned && !processing_entered &&
                         !processing_returned;
    if (!processed && !dropped) {
      throw ReplayError(CP2OfflineReplayStatus::kInvalidArtifact,
                        "serial pair enqueue/processing transition is invalid");
    }
    const std::vector<std::uint64_t> ids = as_u64_array(
        required(row, "updater_invocation_ids"), "updater invocation IDs");
    if ((!processed && !ids.empty())) {
      throw ReplayError(CP2OfflineReplayStatus::kInvalidArtifact,
                        "unprocessed serial pair owns updater invocations");
    }
    const std::uint64_t timestamp =
        as_u64(required(row, "camera_timestamp_ns"), "camera timestamp");
    for (std::uint64_t invocation : ids) {
      if (!invocations
               .emplace(std::make_tuple(sequence, pair, invocation), timestamp)
               .second) {
        throw ReplayError(CP2OfflineReplayStatus::kInvalidArtifact,
                          "updater invocation appears in multiple serial pairs");
      }
    }
  }
  return invocations;
}

void validate_zero_raw_update(const std::map<std::string, JsonValue> &row) {
  const std::vector<std::uint64_t> empty;
  const ov_msckf::CP2AcceptedFeatureDigests digest =
      ov_msckf::CP2TraceCodec::AcceptedFeatureDigests(empty);
  const ov_msckf::CP2TracePayloadReference absent;
  const std::string terminal =
      as_string(required(row, "terminal_status"), "terminal status");
  const std::string subreason =
      as_string(required(row, "terminal_subreason"), "terminal subreason");
  const std::uint64_t input_feature_count =
      as_u64(required(row, "input_feature_count"), "input feature count");
  ov_msckf::cp2_offline_replay_internal::ZeroRawAuxiliaryEvidence auxiliary;
  auxiliary.row_counts_absent =
      nullable(required(row, "baseline_precompression_rows")) &&
      nullable(required(row, "baseline_compressed_rows")) &&
      nullable(required(row, "candidate_precompression_rows")) &&
      nullable(required(row, "candidate_compressed_rows"));
  auxiliary.preview_fields_absent =
      nullable(required(row, "baseline_preview_status")) &&
      nullable(required(row, "baseline_preview_stage")) &&
      nullable(required(row, "candidate_preview_status")) &&
      nullable(required(row, "candidate_preview_stage"));
  auxiliary.candidate_outcome_not_reached =
      as_string(required(row, "candidate_outcome"), "candidate outcome") ==
      "not_reached";
  auxiliary.preview_counters_zero =
      counter_object_equal(required(row, "baseline_preview_counters"), 0U,
                           0U, 0U, 0U, 0U, 0U, 0U) &&
      counter_object_equal(required(row, "candidate_preview_counters"), 0U,
                           0U, 0U, 0U, 0U, 0U, 0U);
  auxiliary.candidate_proposal_unavailable =
      !as_bool(required(row, "candidate_proposal_available"),
               "candidate proposal available");
  auxiliary.baseline_commit_oracle_counters_zero =
      as_u64(required(row, "baseline_commit_count"),
             "baseline commit count") == 0U &&
      as_u64(required(row, "baseline_transaction_mean_commits"),
             "baseline transaction mean commits") == 0U &&
      as_u64(required(row, "baseline_covariance_commits"),
             "baseline covariance commits") == 0U &&
      as_u64(required(row, "baseline_expected_type_update_calls"),
             "baseline expected Type calls") == 0U &&
      as_u64(required(row, "baseline_verified_nominal_fields"),
             "baseline verified nominal fields") == 0U &&
      as_u64(required(row, "baseline_nominal_mismatches"),
             "baseline nominal mismatches") == 0U &&
      as_u64(required(row, "baseline_covariance_mismatches"),
             "baseline covariance mismatches") == 0U &&
      as_u64(required(row, "baseline_fej_mismatches"),
             "baseline FEJ mismatches") == 0U;
  auxiliary.candidate_write_counters_zero =
      as_u64(required(row, "candidate_ekf_update_calls"),
             "candidate EKF calls") == 0U &&
      as_u64(required(row, "candidate_type_update_calls"),
             "candidate Type calls") == 0U &&
      as_u64(required(row, "candidate_mean_writes"),
             "candidate mean writes") == 0U &&
      as_u64(required(row, "candidate_covariance_writes"),
             "candidate covariance writes") == 0U &&
      as_u64(required(row, "candidate_feature_writes"),
             "candidate feature writes") == 0U;
  if (!ov_msckf::cp2_offline_replay_internal::ZeroRawTerminalPasses(
          terminal, subreason, input_feature_count) ||
      !ov_msckf::cp2_offline_replay_internal::
          ZeroRawAuxiliaryEvidencePasses(auxiliary) ||
      !as_bool(required(row, "zero_write_snapshot_equal"),
               "zero write snapshot equality") ||
      !as_u64_array(required(row, "baseline_accepted_ids"),
                    "baseline accepted IDs").empty() ||
      !as_u64_array(required(row, "candidate_accepted_ids"),
                    "candidate accepted IDs").empty() ||
      as_string(required(row, "baseline_accepted_set_sha256"),
                "baseline set digest") != digest.set_sha256 ||
      as_string(required(row, "baseline_accepted_sequence_sha256"),
                "baseline sequence digest") != digest.sequence_sha256 ||
      as_string(required(row, "candidate_accepted_set_sha256"),
                "candidate set digest") != digest.set_sha256 ||
      as_string(required(row, "candidate_accepted_sequence_sha256"),
                "candidate sequence digest") != digest.sequence_sha256 ||
      as_string(required(row, "baseline_gamma_status"),
                "baseline gamma status") != "not_reached" ||
      !nullable(required(row, "baseline_gamma")) ||
      !nullable(required(row, "candidate_gamma")) ||
      !payload_reference_equal(row, "prior_snapshot_sha256",
                               "prior_payload_offset", "prior_payload_length",
                               false, absent) ||
      !payload_reference_equal(row, "precommit_snapshot_sha256",
                               "precommit_payload_offset",
                               "precommit_payload_length", false, absent) ||
      !payload_reference_equal(
          row, "expected_postcommit_snapshot_sha256",
          "expected_postcommit_payload_offset",
          "expected_postcommit_payload_length", false, absent) ||
      !payload_reference_equal(row, "live_postcommit_snapshot_sha256",
                               "live_postcommit_payload_offset",
                               "live_postcommit_payload_length", false, absent) ||
      !payload_reference_equal(
          row, "baseline_proposal_sha256", "baseline_proposal_payload_offset",
          "baseline_proposal_payload_length", false, absent) ||
      !payload_reference_equal(
          row, "candidate_proposal_sha256", "candidate_proposal_payload_offset",
          "candidate_proposal_payload_length", false, absent) ||
      as_u64(required(row, "state_block_rows"), "state block rows") != 0U ||
      as_u64(required(row, "covariance_block_rows"),
             "covariance block rows") != 0U ||
      !as_bool(required(row, "all_block_rows_present"),
               "all block rows present")) {
    throw ReplayError(CP2OfflineReplayStatus::kInvalidArtifact,
                      "zero-raw update violates its absence contract");
  }
}

void write_report_file(int descriptor, const std::string &report) {
  if (descriptor < 0 || ::lseek(descriptor, 0, SEEK_SET) != 0) {
    throw ReplayError(CP2OfflineReplayStatus::kOutputFailure,
                      "output file cannot be positioned");
  }
  std::size_t offset = 0U;
  while (offset < report.size()) {
    ssize_t amount = -1;
    do {
      amount = ::write(descriptor, report.data() + offset, report.size() - offset);
    } while (amount < 0 && errno == EINTR);
    if (amount <= 0) {
      throw ReplayError(CP2OfflineReplayStatus::kOutputFailure,
                        "output report could not be written completely");
    }
    offset += static_cast<std::size_t>(amount);
  }
  if (::fsync(descriptor) != 0) {
    throw ReplayError(CP2OfflineReplayStatus::kOutputFailure,
                      "output report could not be fsynced");
  }
  struct stat status {};
  if (::fstat(descriptor, &status) != 0 || !S_ISREG(status.st_mode) ||
      status.st_nlink != 1 || status.st_size < 0 ||
      static_cast<std::uint64_t>(status.st_size) != report.size()) {
    throw ReplayError(CP2OfflineReplayStatus::kOutputFailure,
                      "output report identity/size changed while writing");
  }
}

struct ReplayReport {
  std::string source_commit;
  std::string source_tree;
  std::string executable_sha256;
  std::string executable_build_id;
  std::map<std::string, std::string> input_sha256;
  std::vector<ResolvedParameterDigest> resolved_parameters;
  std::uint64_t replayed_invocations = 0U;
  std::uint64_t replayed_raw_systems = 0U;
  std::uint64_t baseline_expected_proposals = 0U;
  std::uint64_t candidate_expected_proposals = 0U;
  std::uint64_t baseline_exact_proposal_matches = 0U;
  std::uint64_t candidate_exact_proposal_matches = 0U;
  FailureCounts failures;

  bool passed() const noexcept {
    return replayed_invocations != 0U && replayed_raw_systems != 0U &&
           baseline_expected_proposals == baseline_exact_proposal_matches &&
           candidate_expected_proposals == candidate_exact_proposal_matches &&
           failures.empty();
  }
};

std::string report_json(const ReplayReport &report) {
  static const std::array<const char *, 8U> input_names = {{
      "serial_pairs", "updates", "features", "state_blocks",
      "covariance_blocks", "state_snapshot_payloads", "proposal_payloads",
      "raw_system_payloads"}};
  std::ostringstream output;
  output.imbue(std::locale::classic());
  output << "{\"schema_version\":1,\"record_type\":\"cp2_offline_replay\""
         << ",\"checkpoint\":\"CP2-C\",\"source_commit\":"
         << json_string(report.source_commit) << ",\"source_tree\":"
         << json_string(report.source_tree) << ",\"executable_sha256\":"
         << json_string(report.executable_sha256)
         << ",\"executable_build_id\":"
         << json_string(report.executable_build_id)
         << ",\"strict_fp_verified\":true,\"bag_provider_calls\":0"
         << ",\"input_sha256\":{";
  for (std::size_t index = 0U; index < input_names.size(); ++index) {
    if (index != 0U) {
      output << ',';
    }
    const auto found = report.input_sha256.find(input_names[index]);
    if (found == report.input_sha256.end()) {
      throw ReplayError(CP2OfflineReplayStatus::kReplayFailed,
                        "report input hash population is incomplete");
    }
    output << json_string(input_names[index]) << ':' << json_string(found->second);
  }
  output << "},\"resolved_parameters\":[";
  for (std::size_t index = 0U; index < report.resolved_parameters.size(); ++index) {
    if (index != 0U) {
      output << ',';
    }
    output << "{\"sequence_index\":"
           << report.resolved_parameters[index].sequence_index
           << ",\"sha256\":"
           << json_string(report.resolved_parameters[index].sha256) << '}';
  }
  output << "],\"replayed_invocations\":" << report.replayed_invocations
         << ",\"replayed_raw_systems\":" << report.replayed_raw_systems
         << ",\"baseline_expected_proposals\":"
         << report.baseline_expected_proposals
         << ",\"candidate_expected_proposals\":"
         << report.candidate_expected_proposals
         << ",\"baseline_exact_proposal_matches\":"
         << report.baseline_exact_proposal_matches
         << ",\"candidate_exact_proposal_matches\":"
         << report.candidate_exact_proposal_matches
         << ",\"failure_counts\":{"
         << "\"layout\":" << report.failures.layout
         << ",\"reduction\":" << report.failures.reduction
         << ",\"statistics\":" << report.failures.statistics
         << ",\"gate\":" << report.failures.gate
         << ",\"accepted_sequence\":" << report.failures.accepted_sequence
         << ",\"gamma\":" << report.failures.gamma
         << ",\"stack\":" << report.failures.stack
         << ",\"compression\":" << report.failures.compression
         << ",\"preview_status\":" << report.failures.preview_status
         << ",\"proposal_presence\":" << report.failures.proposal_presence
         << ",\"proposal_bytes\":" << report.failures.proposal_bytes
         << ",\"commit_oracle\":" << report.failures.commit_oracle
         << ",\"block_metrics\":" << report.failures.block_metrics
         << "},\"passed\":" << (report.passed() ? "true" : "false") << "}\n";
  return output.str();
}

ReplayReport perform_replay(const ov_msckf::CP2OfflineReplayPaths &paths,
                            int artifact_directory) {
  static const std::array<const char *, 8U> file_names = {{
      "serial_pairs.jsonl", "updates.jsonl", "features.jsonl",
      "state_blocks.jsonl", "covariance_blocks.jsonl",
      "state_snapshot_payloads.bin", "proposal_payloads.bin",
      "raw_system_payloads.bin"}};
  std::array<FileBytes, 8U> files;
  for (std::size_t index = 0U; index < file_names.size(); ++index) {
    files[index] = read_artifact_file(
        artifact_directory, file_names[index],
        index < 5U ? kMaximumJsonBytes : kMaximumBinaryBytes);
  }
  const bool have_provenance =
      artifact_file_exists(artifact_directory, "provenance.json");
  const bool have_replay_input =
      artifact_file_exists(artifact_directory, "replay_input.json");
  if (have_provenance == have_replay_input) {
    throw ReplayError(
        CP2OfflineReplayStatus::kInvalidArtifact,
        "artifact must contain exactly one of provenance.json or transient replay_input.json");
  }
  const FileBytes identity_file = read_artifact_file(
      artifact_directory,
      have_provenance ? "provenance.json" : "replay_input.json",
      kMaximumJsonBytes);
  const ProvenanceData provenance =
      have_provenance ? parse_provenance(artifact_directory, identity_file)
                      : parse_replay_input(artifact_directory, identity_file);

  OwnedDescriptor executable = open_absolute(paths.executable_path, O_RDONLY, false);
  const FileBytes executable_file = read_file(executable.get(), kMaximumBinaryBytes);
  if (executable_file.sha256 != provenance.executable_sha256 ||
      executable_build_id(executable_file.bytes) !=
          provenance.executable_build_id) {
    throw ReplayError(CP2OfflineReplayStatus::kInvalidArtifact,
                      "running executable hash/build ID differs from provenance");
  }

  ReplayReport report;
  report.source_commit = provenance.source_commit;
  report.source_tree = provenance.source_tree;
  report.executable_sha256 = provenance.executable_sha256;
  report.executable_build_id = provenance.executable_build_id;
  report.resolved_parameters = provenance.resolved_parameters;
  static const std::array<const char *, 8U> hash_names = {{
      "serial_pairs", "updates", "features", "state_blocks",
      "covariance_blocks", "state_snapshot_payloads", "proposal_payloads",
      "raw_system_payloads"}};
  for (std::size_t index = 0U; index < files.size(); ++index) {
    report.input_sha256.emplace(hash_names[index], files[index].sha256);
  }

  const std::vector<JsonValue> serial_values = parse_json_lines(files[0]);
  const std::vector<JsonValue> update_values = parse_json_lines(files[1]);
  const std::vector<JsonValue> feature_values = parse_json_lines(files[2]);
  const std::vector<JsonValue> state_block_values = parse_json_lines(files[3]);
  const std::vector<JsonValue> covariance_block_values =
      parse_json_lines(files[4]);
  const std::map<TraceKey, std::uint64_t> serial_invocations =
      validate_serial_pairs(serial_values);

  std::vector<ov_msckf::CP2StateTraceFrame> decoded_states =
      ov_msckf::CP2StateTraceCodec::DecodeStateFile(files[5].bytes);
  std::vector<ov_msckf::CP2ProposalTraceFrame> decoded_proposals =
      ov_msckf::CP2TraceCodec::DecodeProposalFile(files[6].bytes);
  std::vector<ov_msckf::CP2RawSystemTraceFrame> decoded_raw =
      ov_msckf::CP2TraceCodec::DecodeRawSystemFile(files[7].bytes);
  std::map<TraceKey, std::vector<ov_msckf::CP2StateTraceFrame>> states;
  std::map<TraceKey, std::vector<ov_msckf::CP2ProposalTraceFrame>> proposals;
  std::map<TraceKey, std::vector<ov_msckf::CP2RawSystemTraceFrame>> raw_frames;
  for (const ov_msckf::CP2StateTraceFrame &frame : decoded_states) {
    states[trace_key(frame.invocation)].push_back(frame);
  }
  for (const ov_msckf::CP2ProposalTraceFrame &frame : decoded_proposals) {
    proposals[trace_key(frame.invocation)].push_back(frame);
  }
  for (const ov_msckf::CP2RawSystemTraceFrame &frame : decoded_raw) {
    raw_frames[trace_key(frame.invocation)].push_back(frame);
  }

  std::map<TraceKey, std::vector<JsonRowPointer>> feature_rows;
  for (const JsonValue &value : feature_values) {
    const auto &row = as_object(value, "feature row");
    require_exact_keys(row, feature_keys(), "feature row");
    require_schema_record(row, "feature_comparison");
    feature_rows[trace_key(identity_from_row(row))].push_back(&row);
  }
  std::map<TraceKey, std::vector<JsonRowPointer>> state_block_rows;
  for (const JsonValue &value : state_block_values) {
    const auto &row = as_object(value, "state block row");
    require_exact_keys(row, state_block_keys(), "state block row");
    require_schema_record(row, "state_block_comparison");
    state_block_rows[trace_key(identity_from_row(row))].push_back(&row);
  }
  std::map<TraceKey, std::vector<JsonRowPointer>> covariance_block_rows;
  for (const JsonValue &value : covariance_block_values) {
    const auto &row = as_object(value, "covariance block row");
    require_exact_keys(row, covariance_block_keys(), "covariance block row");
    require_schema_record(row, "covariance_block_comparison");
    covariance_block_rows[trace_key(identity_from_row(row))].push_back(&row);
  }

  std::vector<UpdateView> updates;
  updates.reserve(update_values.size());
  std::map<TraceKey, std::size_t> update_keys_seen;
  std::map<std::uint64_t, std::uint64_t> next_invocation;
  Identity previous_identity;
  bool have_previous_identity = false;
  for (const JsonValue &value : update_values) {
    const auto &row = as_object(value, "update row");
    require_exact_keys(row, update_keys(), "update row");
    require_schema_record(row, "updater_invocation");
    UpdateView update;
    update.identity = identity_from_row(row);
    update.row = &row;
    const std::uint64_t input_feature_count =
        as_u64(required(row, "input_feature_count"), "input feature count");
    update.raw_system_count =
        as_u64(required(row, "raw_system_count"), "raw system count");
    (void)as_u64(required(row, "duration_ns"), "duration");
    update.committed =
        as_string(required(row, "terminal_status"), "terminal status") ==
        "committed_counted";
    if ((have_previous_identity && !(previous_identity < update.identity)) ||
        update.identity.invocation_id !=
            next_invocation[update.identity.sequence_index] ||
        update.identity.sequence_index >= report.resolved_parameters.size() ||
        !ov_msckf::cp2_offline_replay_internal::
            FrozenSequenceIdentityPasses(
                update.identity.sequence_index,
                as_string(required(row, "sequence_id"), "sequence ID")) ||
        !ov_msckf::cp2_offline_replay_internal::UpdatePopulationPasses(
            input_feature_count, update.raw_system_count) ||
        as_string(required(row, "resolved_parameters_sha256"),
                  "resolved parameter hash") !=
            report.resolved_parameters[update.identity.sequence_index].sha256 ||
        as_string(required(row, "live_mode"), "live mode") != "nullspace" ||
        nullable(required(row, "shadow_mode")) ||
        as_string(required(row, "shadow_mode"), "shadow mode") != "schur" ||
        !as_bool(required(row, "shadow_enabled"), "shadow enabled") ||
        as_bool(required(row, "timing_evidence_eligible"),
                "timing evidence eligibility")) {
      throw ReplayError(CP2OfflineReplayStatus::kInvalidArtifact,
                        "update identity/mode/parameter join is invalid");
    }
    increment(next_invocation[update.identity.sequence_index], "invocation ID");
    const TraceKey key = trace_key(update.identity);
    const auto serial = serial_invocations.find(key);
    if (serial == serial_invocations.end() ||
        serial->second != update.identity.camera_timestamp_ns ||
        !update_keys_seen.emplace(key, updates.size()).second) {
      throw ReplayError(CP2OfflineReplayStatus::kInvalidArtifact,
                        "update does not join uniquely to a processed pair");
    }
    static const std::array<const char *, 4U> hash_fields = {{
        "config_sha256", "bag_sha256", "pair_index_sha256",
        "resolved_parameters_sha256"}};
    for (const char *field : hash_fields) {
      if (!valid_sha256(as_string(required(row, field), field))) {
        throw ReplayError(CP2OfflineReplayStatus::kInvalidArtifact,
                          "update contains an invalid joined SHA-256");
      }
    }
    updates.push_back(update);
    previous_identity = update.identity;
    have_previous_identity = true;
  }
  if (updates.empty() || update_keys_seen.size() != serial_invocations.size()) {
    throw ReplayError(CP2OfflineReplayStatus::kInvalidArtifact,
                      "update/serial invocation populations differ");
  }

  const auto no_unknown_groups = [&update_keys_seen](const auto &groups,
                                                      const char *what) {
    for (const auto &entry : groups) {
      if (update_keys_seen.count(entry.first) != 1U) {
        throw ReplayError(CP2OfflineReplayStatus::kInvalidArtifact,
                          std::string(what) + " references an unknown update");
      }
    }
  };
  no_unknown_groups(states, "state payload");
  no_unknown_groups(proposals, "proposal payload");
  no_unknown_groups(raw_frames, "raw payload");
  no_unknown_groups(feature_rows, "feature row");
  no_unknown_groups(state_block_rows, "state block row");
  no_unknown_groups(covariance_block_rows, "covariance block row");

  const ov_msckf::CP2FeatureGate::ChiSquaredTable chi_table =
      chi_squared_table();
  ov_msckf::CP2TraceReplayInput replay_input;
  replay_input.raw_system_file_bytes = files[7].bytes;
  replay_input.proposal_file_bytes = files[6].bytes;
  replay_input.chi_squared_table = chi_table;
  std::uint64_t expected_nonzero_invocations = 0U;
  std::uint64_t expected_raw_systems = 0U;
  for (const UpdateView &update : updates) {
    const TraceKey key = trace_key(update.identity);
    const auto &row = *update.row;
    const auto raw = raw_frames.find(key);
    const auto state = states.find(key);
    const auto proposal = proposals.find(key);
    const auto features = feature_rows.find(key);
    const auto state_rows = state_block_rows.find(key);
    const auto covariance_rows = covariance_block_rows.find(key);
    if (update.raw_system_count == 0U) {
      validate_zero_raw_update(row);
      const bool recorded_math_passed =
          as_bool(required(row, "math_passed"), "math passed");
      if (!ov_msckf::cp2_offline_replay_internal::
              ZeroRawMathEvidencePasses(recorded_math_passed)) {
        increment(report.failures.preview_status,
                  "zero-raw math-evidence failure");
      }
      if (raw != raw_frames.end() || state != states.end() ||
          proposal != proposals.end() || features != feature_rows.end() ||
          state_rows != state_block_rows.end() ||
          covariance_rows != covariance_block_rows.end()) {
        throw ReplayError(CP2OfflineReplayStatus::kInvalidArtifact,
                          "zero-raw update owns replay payload/rows");
      }
      continue;
    }
    increment(expected_nonzero_invocations, "nonzero invocation");
    std::uint64_t next_raw_total = 0U;
    if (!checked_add(expected_raw_systems, update.raw_system_count,
                     next_raw_total)) {
      throw ReplayError(CP2OfflineReplayStatus::kReplayFailed,
                        "raw-system population overflows u64");
    }
    expected_raw_systems = next_raw_total;
    if (raw == raw_frames.end() || state == states.end() ||
        features == feature_rows.end() ||
        raw->second.size() != update.raw_system_count ||
        features->second.size() != update.raw_system_count ||
        (state->second.size() != 2U && state->second.size() != 4U)) {
      throw ReplayError(CP2OfflineReplayStatus::kInvalidArtifact,
                        "nonzero update has an incomplete replay population");
    }
    const std::vector<ov_msckf::CP2ProposalTraceFrame> empty_proposals;
    const std::vector<ov_msckf::CP2ProposalTraceFrame> &joined_proposals =
        proposal == proposals.end() ? empty_proposals : proposal->second;
    ov_msckf::MSCKFUpdatePreviewSnapshot prior;
    if (ov_msckf::CP2CompositeStateAdapter::ProjectPreview(
            state->second[0].snapshot, prior) !=
        ov_msckf::CP2CompositeStateStatus::kAccepted) {
      throw ReplayError(CP2OfflineReplayStatus::kInvalidArtifact,
                        "phase-0 snapshot cannot be projected for replay");
    }
    const ResolvedParameterDigest &parameters =
        report.resolved_parameters[update.identity.sequence_index];
    replay_input.invocation = trace_identity(update.identity);
    replay_input.prior = std::move(prior);
    replay_input.raw_frames = raw->second;
    replay_input.proposal_frames = joined_proposals;
    replay_input.sigma_px = parameters.sigma_px;
    replay_input.sigma_px_sq = parameters.sigma_px * parameters.sigma_px;
    replay_input.chi2_multiplier = parameters.chi2_multiplier;
    const ov_msckf::CP2TraceReplayResult replay =
        ov_msckf::CP2TraceCodec::ReplayDecodedInvocation(
            replay_input, decoded_raw, decoded_proposals);
    increment(report.replayed_invocations, "replayed invocation");
    for (std::size_t index = 0U; index < raw->second.size(); ++index) {
      increment(report.replayed_raw_systems, "replayed raw system");
      const auto &feature_row = *features->second[index];
      if (!repeated_update_fields_equal(feature_row, row) ||
          as_u64(required(feature_row, "feature_ordinal"),
                 "feature ordinal") != index) {
        increment(report.failures.layout, "layout failure");
      }
      compare_feature_row(feature_row, update.identity, raw->second[index],
                          replay.math.features[index],
                          replay.baseline_accepted,
                          replay.candidate_accepted, replay.math.nullspace,
                          replay.math.schur, state->second[0].payload,
                          report.failures);
    }
    compare_update_row(row, replay, state->second, joined_proposals,
                       report.failures);
    compare_detached_commit(row, replay, state->second, report.failures);

    const std::vector<JsonRowPointer> no_rows;
    const std::vector<JsonRowPointer> &joined_state_rows =
        state_rows == state_block_rows.end() ? no_rows : state_rows->second;
    const std::vector<JsonRowPointer> &joined_covariance_rows =
        covariance_rows == covariance_block_rows.end()
            ? no_rows
            : covariance_rows->second;
    if (replay.math.nullspace.proposal_available) {
      compare_block_rows(update.identity, row, state->second[0].snapshot,
                         replay.math.nullspace, replay.math.schur,
                         joined_state_rows, joined_covariance_rows,
                         report.failures);
    } else if (!joined_state_rows.empty() || !joined_covariance_rows.empty()) {
      increment(report.failures.block_metrics, "block-metric failure");
    }

    const auto role_reference = [&joined_proposals](
                                    ov_msckf::CP2ProposalTraceRole role)
        -> const ov_msckf::CP2TracePayloadReference * {
      for (const ov_msckf::CP2ProposalTraceFrame &frame : joined_proposals) {
        if (frame.role == role) {
          return &frame.payload;
        }
      }
      return nullptr;
    };
    if (replay.math.nullspace.proposal_available) {
      increment(report.baseline_expected_proposals,
                "baseline expected proposal");
      const auto *reference = role_reference(
          ov_msckf::CP2ProposalTraceRole::kNullspaceBaseline);
      if (reference != nullptr && payload_reference_equal(
                                    row, "baseline_proposal_sha256",
                                    "baseline_proposal_payload_offset",
                                    "baseline_proposal_payload_length", true,
                                    *reference)) {
        increment(report.baseline_exact_proposal_matches,
                  "baseline proposal match");
      }
    }
    if (replay.math.schur.proposal_available) {
      increment(report.candidate_expected_proposals,
                "candidate expected proposal");
      const auto *reference =
          role_reference(ov_msckf::CP2ProposalTraceRole::kSchurCandidate);
      if (reference != nullptr && payload_reference_equal(
                                    row, "candidate_proposal_sha256",
                                    "candidate_proposal_payload_offset",
                                    "candidate_proposal_payload_length", true,
                                    *reference)) {
        increment(report.candidate_exact_proposal_matches,
                  "candidate proposal match");
      }
    }
    const bool recorded_math_passed =
        as_bool(required(row, "math_passed"), "math passed");
    if (!ov_msckf::cp2_offline_replay_internal::
            RecordedAndReplayedMathEvidencePasses(recorded_math_passed,
                                                  replay.math)) {
      increment(report.failures.preview_status, "math-evidence failure");
    }
  }
  if (report.replayed_invocations != expected_nonzero_invocations ||
      report.replayed_raw_systems != expected_raw_systems ||
      report.replayed_raw_systems != feature_values.size()) {
    throw ReplayError(CP2OfflineReplayStatus::kReplayFailed,
                      "replay population reconciliation failed");
  }
  return report;
}

} // namespace

const char *ov_msckf::cp2_offline_replay_status_name(
    CP2OfflineReplayStatus status) noexcept {
  switch (status) {
  case CP2OfflineReplayStatus::kAccepted: return "accepted";
  case CP2OfflineReplayStatus::kInvalidArguments: return "invalid_arguments";
  case CP2OfflineReplayStatus::kInvalidPath: return "invalid_path";
  case CP2OfflineReplayStatus::kInvalidArtifact: return "invalid_artifact";
  case CP2OfflineReplayStatus::kReplayFailed: return "replay_failed";
  case CP2OfflineReplayStatus::kOutputFailure: return "output_failure";
  case CP2OfflineReplayStatus::kAllocationFailure: return "allocation_failure";
  }
  return "unknown";
}

ov_msckf::CP2OfflineReplayResult
ov_msckf::ParseCP2OfflineReplayArguments(
    int argc, const char *const *argv, CP2OfflineReplayPaths &paths) noexcept {
  CP2OfflineReplayResult result;
  paths = CP2OfflineReplayPaths{};
  try {
    if (argc != 5 || argv == nullptr || argv[0] == nullptr ||
        argv[1] == nullptr || argv[2] == nullptr || argv[3] == nullptr ||
        argv[4] == nullptr ||
        std::string(argv[1]) != "--cp2-offline-replay" ||
        std::string(argv[3]) != "--output") {
      result.status = CP2OfflineReplayStatus::kInvalidArguments;
      result.detail = "expected --cp2-offline-replay ABS_ARTIFACT --output ABS_REPLAY_REPORT";
      return result;
    }
    CP2OfflineReplayPaths parsed;
    parsed.executable_path = argv[0];
    parsed.artifact_directory = argv[2];
    parsed.output_path = argv[4];
    if (!normalized_absolute_path(parsed.executable_path) ||
        !normalized_absolute_path(parsed.artifact_directory) ||
        !normalized_absolute_path(parsed.output_path) ||
        parsed.executable_path == parsed.artifact_directory ||
        parsed.executable_path == parsed.output_path ||
        parsed.artifact_directory == parsed.output_path) {
      result.status = CP2OfflineReplayStatus::kInvalidPath;
      result.detail = "offline replay paths must be distinct normalized absolute paths";
      return result;
    }
    paths = std::move(parsed);
    result.status = CP2OfflineReplayStatus::kAccepted;
    return result;
  } catch (const std::bad_alloc &) {
    result.status = CP2OfflineReplayStatus::kAllocationFailure;
    result.detail = "offline replay argument allocation failed";
  } catch (...) {
    result.status = CP2OfflineReplayStatus::kInvalidArguments;
    result.detail = "offline replay arguments are invalid";
  }
  return result;
}

ov_msckf::CP2OfflineReplayResult ov_msckf::RunCP2OfflineReplay(
    const CP2OfflineReplayPaths &paths) noexcept {
  CP2OfflineReplayResult result;
  try {
    if (!normalized_absolute_path(paths.executable_path) ||
        !normalized_absolute_path(paths.artifact_directory) ||
        !normalized_absolute_path(paths.output_path) ||
        paths.executable_path == paths.artifact_directory ||
        paths.executable_path == paths.output_path ||
        paths.artifact_directory == paths.output_path) {
      throw ReplayError(CP2OfflineReplayStatus::kInvalidPath,
                        "offline replay paths are not normalized absolute paths");
    }
    OwnedDescriptor output = open_absolute(paths.output_path, O_RDWR, false);
    require_regular_single(output.get(), true);
    OwnedDescriptor artifact =
        open_absolute(paths.artifact_directory, O_RDONLY, true);
    ReplayReport report = perform_replay(paths, artifact.get());
    const std::string bytes = report_json(report);
    write_report_file(output.get(), bytes);
    if (!report.passed()) {
      result.status = CP2OfflineReplayStatus::kReplayFailed;
      result.detail = "offline replay completed with mathematical mismatches";
      return result;
    }
    result.status = CP2OfflineReplayStatus::kAccepted;
    return result;
  } catch (const ReplayError &error) {
    result.status = error.status();
    result.detail = error.what();
  } catch (const std::bad_alloc &) {
    result.status = CP2OfflineReplayStatus::kAllocationFailure;
    result.detail = "offline replay allocation failed";
  } catch (const std::exception &error) {
    result.status = CP2OfflineReplayStatus::kReplayFailed;
    result.detail = error.what();
  } catch (...) {
    result.status = CP2OfflineReplayStatus::kReplayFailed;
    result.detail = "offline replay failed with an unknown exception";
  }
  return result;
}
