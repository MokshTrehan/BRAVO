/*
 * SchurVIO-Lite CP2 recorded artifact assembler.
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include "CP2Canonical.h"
#include "CP2CommitOracle.h"
#include "CP2CompositeState.h"
#include "CP2StateTraceCodec.h"
#include "CP2TraceCodec.h"
#include "CP2TraceJournal.h"

#include <Eigen/Core>

#include <algorithm>
#include <array>
#include <cerrno>
#include <cfenv>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <dirent.h>
#include <fcntl.h>
#include <iomanip>
#include <limits>
#include <locale>
#include <map>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <sys/stat.h>
#include <tuple>
#include <unistd.h>
#include <utility>
#include <vector>

namespace {

constexpr std::uint64_t kMaximumJsonBytes = UINT64_C(1073741824);
constexpr std::uint64_t kMaximumJournalBytes = UINT64_C(2147483648);
constexpr std::size_t kMaximumJsonDepth = 64U;
constexpr std::uint64_t kMaximumJsonNodes = UINT64_C(50000000);
constexpr std::uint64_t kMinimumCommittingUpdates = UINT64_C(1000);

const std::array<const char *, 3> kSequenceIds{{
    "MH_01_easy", "MH_03_medium", "V1_01_easy"}};

class AssembleError final : public std::runtime_error {
public:
  explicit AssembleError(const std::string &message)
      : std::runtime_error(message) {}
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

std::uint64_t add_or_fail(std::uint64_t left, std::uint64_t right,
                          const char *label) {
  std::uint64_t result = 0U;
  if (!checked_add(left, right, result)) {
    throw AssembleError(std::string(label) + " overflows u64");
  }
  return result;
}

std::uint64_t multiply_or_fail(std::uint64_t left, std::uint64_t right,
                               const char *label) {
  std::uint64_t result = 0U;
  if (!ov_msckf::cp2_checked_multiply_u64(left, right, result)) {
    throw AssembleError(std::string(label) + " overflows u64");
  }
  return result;
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
    if (length == 0U || (length == 1U && path[begin] == '.') ||
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

std::vector<std::string> path_components(const std::string &path) {
  std::vector<std::string> result;
  std::size_t begin = 1U;
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
                              bool directory) {
  if (!normalized_absolute_path(path)) {
    throw AssembleError("path is not normalized absolute");
  }
  OwnedDescriptor current(::open("/", O_RDONLY | O_DIRECTORY | O_CLOEXEC));
  if (!current.valid()) {
    throw AssembleError("cannot open filesystem root");
  }
  const std::vector<std::string> components = path_components(path);
  for (std::size_t index = 0U; index < components.size(); ++index) {
    const bool leaf = index + 1U == components.size();
    int flags = O_CLOEXEC | O_NOFOLLOW;
    if (!leaf || directory) {
      flags |= O_DIRECTORY;
    }
    flags |= leaf ? leaf_flags : O_RDONLY;
    const int descriptor =
        ::openat(current.get(), components[index].c_str(), flags);
    if (descriptor < 0) {
      throw AssembleError("path cannot be opened without following links");
    }
    current = OwnedDescriptor(descriptor);
  }
  return current;
}

struct FileIdentity {
  dev_t device = 0;
  ino_t inode = 0;
  off_t size = 0;
  timespec modified{};
  timespec changed{};
};

FileIdentity file_identity(int descriptor, bool directory = false) {
  struct stat status {};
  if (::fstat(descriptor, &status) != 0 ||
      (!directory && status.st_nlink != 1) ||
      (directory ? !S_ISDIR(status.st_mode) : !S_ISREG(status.st_mode)) ||
      (!directory && status.st_size < 0)) {
    throw AssembleError("input is not a regular single-link file");
  }
  FileIdentity result;
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

bool same_file(const FileIdentity &left, const FileIdentity &right) noexcept {
  return left.device == right.device && left.inode == right.inode;
}

bool unchanged(const FileIdentity &left, const FileIdentity &right) noexcept {
  return same_file(left, right) && left.size == right.size &&
         left.modified.tv_sec == right.modified.tv_sec &&
         left.modified.tv_nsec == right.modified.tv_nsec &&
         left.changed.tv_sec == right.changed.tv_sec &&
         left.changed.tv_nsec == right.changed.tv_nsec;
}

struct FileBytes {
  std::vector<std::uint8_t> bytes;
  std::string sha256;
  FileIdentity identity;
};

FileBytes read_file(int descriptor, std::uint64_t maximum) {
  FileBytes result;
  result.identity = file_identity(descriptor);
  if (static_cast<std::uint64_t>(result.identity.size) > maximum ||
      static_cast<std::uint64_t>(result.identity.size) >
          static_cast<std::uint64_t>(std::numeric_limits<std::size_t>::max()) ||
      ::lseek(descriptor, 0, SEEK_SET) != 0) {
    throw AssembleError("input exceeds its size bound or is not seekable");
  }
  result.bytes.resize(static_cast<std::size_t>(result.identity.size));
  std::size_t offset = 0U;
  while (offset < result.bytes.size()) {
    ssize_t amount = -1;
    do {
      amount = ::read(descriptor, result.bytes.data() + offset,
                      result.bytes.size() - offset);
    } while (amount < 0 && errno == EINTR);
    if (amount <= 0) {
      throw AssembleError("input could not be read completely");
    }
    offset += static_cast<std::size_t>(amount);
  }
  std::uint8_t extra = 0U;
  ssize_t trailing = -1;
  do {
    trailing = ::read(descriptor, &extra, 1U);
  } while (trailing < 0 && errno == EINTR);
  if (trailing != 0 || !unchanged(result.identity, file_identity(descriptor))) {
    throw AssembleError("input changed while being read");
  }
  ov_msckf::CP2Sha256 digest;
  digest.Update(result.bytes);
  result.sha256 = digest.HexDigest();
  return result;
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

void append_utf8(std::uint32_t value, std::string &output) {
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

enum class JsonKind : std::uint8_t {
  kNull, kBool, kU64, kI64, kDouble, kString, kArray, kObject,
};

struct JsonValue {
  JsonKind kind = JsonKind::kNull;
  bool boolean = false;
  std::uint64_t u64 = 0U;
  std::int64_t i64 = 0;
  double f64 = 0.0;
  std::string string;
  std::vector<JsonValue> array;
  std::map<std::string, JsonValue> object;
};

class StrictJsonParser {
public:
  explicit StrictJsonParser(const std::string &input) : input_(input) {}
  JsonValue Parse() {
    Skip();
    JsonValue result = Value(0U);
    Skip();
    if (position_ != input_.size()) {
      Fail("JSON has trailing bytes");
    }
    return result;
  }

private:
  [[noreturn]] void Fail(const char *message) const {
    throw AssembleError(message);
  }
  void Node() {
    if (nodes_ == kMaximumJsonNodes) {
      Fail("JSON node population exceeds bound");
    }
    ++nodes_;
  }
  void Skip() noexcept {
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
  std::uint32_t Hex4() {
    if (input_.size() - position_ < 4U) {
      Fail("JSON unicode escape is truncated");
    }
    std::uint32_t value = 0U;
    for (std::size_t index = 0U; index < 4U; ++index) {
      const char c = input_[position_ + index];
      std::uint32_t digit = 0U;
      if (c >= '0' && c <= '9') digit = static_cast<std::uint32_t>(c - '0');
      else if (c >= 'a' && c <= 'f') digit = static_cast<std::uint32_t>(c - 'a' + 10);
      else if (c >= 'A' && c <= 'F') digit = static_cast<std::uint32_t>(c - 'A' + 10);
      else Fail("JSON unicode escape is invalid");
      value = (value << 4U) | digit;
    }
    position_ += 4U;
    return value;
  }
  std::string String() {
    if (!Take('"')) Fail("JSON string opening quote is missing");
    std::string output;
    while (position_ < input_.size()) {
      const unsigned char byte = static_cast<unsigned char>(input_[position_++]);
      if (byte == static_cast<unsigned char>('"')) {
        if (!valid_utf8(output)) Fail("JSON string is not UTF-8");
        return output;
      }
      if (byte < UINT8_C(0x20)) Fail("JSON string has a control byte");
      if (byte != static_cast<unsigned char>('\\')) {
        output.push_back(static_cast<char>(byte));
        continue;
      }
      if (position_ >= input_.size()) Fail("JSON escape is truncated");
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
        const std::uint32_t first = Hex4();
        std::uint32_t codepoint = first;
        if (first >= UINT32_C(0xd800) && first <= UINT32_C(0xdbff)) {
          if (position_ + 2U > input_.size() || input_[position_] != '\\' ||
              input_[position_ + 1U] != 'u') Fail("JSON surrogate is incomplete");
          position_ += 2U;
          const std::uint32_t second = Hex4();
          if (second < UINT32_C(0xdc00) || second > UINT32_C(0xdfff))
            Fail("JSON low surrogate is invalid");
          codepoint = UINT32_C(0x10000) + ((first - UINT32_C(0xd800)) << 10U) +
                      (second - UINT32_C(0xdc00));
        } else if (first >= UINT32_C(0xdc00) && first <= UINT32_C(0xdfff)) {
          Fail("JSON contains unpaired low surrogate");
        }
        append_utf8(codepoint, output);
        break;
      }
      default: Fail("JSON escape is invalid");
      }
    }
    Fail("JSON string is unterminated");
  }
  JsonValue Number() {
    const std::size_t begin = position_;
    const bool negative = Take('-');
    if (position_ >= input_.size()) Fail("JSON number is truncated");
    if (input_[position_] == '0') {
      ++position_;
      if (position_ < input_.size() && input_[position_] >= '0' &&
          input_[position_] <= '9') Fail("JSON number has a leading zero");
    } else if (input_[position_] >= '1' && input_[position_] <= '9') {
      while (position_ < input_.size() && input_[position_] >= '0' &&
             input_[position_] <= '9') ++position_;
    } else Fail("JSON number has no integer part");
    bool floating = false;
    if (Take('.')) {
      floating = true;
      const std::size_t start = position_;
      while (position_ < input_.size() && input_[position_] >= '0' &&
             input_[position_] <= '9') ++position_;
      if (start == position_) Fail("JSON fraction is empty");
    }
    if (position_ < input_.size() &&
        (input_[position_] == 'e' || input_[position_] == 'E')) {
      floating = true;
      ++position_;
      if (position_ < input_.size() &&
          (input_[position_] == '+' || input_[position_] == '-')) ++position_;
      const std::size_t start = position_;
      while (position_ < input_.size() && input_[position_] >= '0' &&
             input_[position_] <= '9') ++position_;
      if (start == position_) Fail("JSON exponent is empty");
    }
    const std::string token = input_.substr(begin, position_ - begin);
    JsonValue result;
    if (floating) {
      std::istringstream stream(token);
      stream.imbue(std::locale::classic());
      stream >> std::noskipws >> result.f64;
      if (!stream || !stream.eof() || !std::isfinite(result.f64))
        Fail("JSON float is not finite binary64");
      result.kind = JsonKind::kDouble;
      return result;
    }
    std::uint64_t magnitude = 0U;
    for (std::size_t index = negative ? begin + 1U : begin;
         index < position_; ++index) {
      const std::uint64_t digit = static_cast<std::uint64_t>(input_[index] - '0');
      if (magnitude > (UINT64_MAX - digit) / UINT64_C(10))
        Fail("JSON integer exceeds u64");
      magnitude = magnitude * UINT64_C(10) + digit;
    }
    if (!negative) {
      result.kind = JsonKind::kU64;
      result.u64 = magnitude;
      return result;
    }
    const std::uint64_t minimum = UINT64_C(1) << 63U;
    if (magnitude > minimum) Fail("JSON integer is below i64");
    result.kind = JsonKind::kI64;
    result.i64 = magnitude == minimum ? std::numeric_limits<std::int64_t>::min()
                                      : -static_cast<std::int64_t>(magnitude);
    return result;
  }
  JsonValue Value(std::size_t depth) {
    if (depth > kMaximumJsonDepth) Fail("JSON nesting exceeds bound");
    Node();
    if (position_ >= input_.size()) Fail("JSON value is missing");
    JsonValue result;
    if (input_[position_] == '"') {
      result.kind = JsonKind::kString;
      result.string = String();
      return result;
    }
    if (input_.compare(position_, 4U, "null") == 0) {
      position_ += 4U; return result;
    }
    if (input_.compare(position_, 4U, "true") == 0) {
      position_ += 4U; result.kind = JsonKind::kBool; result.boolean = true; return result;
    }
    if (input_.compare(position_, 5U, "false") == 0) {
      position_ += 5U; result.kind = JsonKind::kBool; return result;
    }
    if (input_[position_] == '-' ||
        (input_[position_] >= '0' && input_[position_] <= '9')) return Number();
    if (Take('[')) {
      result.kind = JsonKind::kArray;
      Skip();
      if (Take(']')) return result;
      while (true) {
        result.array.push_back(Value(depth + 1U));
        Skip();
        if (Take(']')) return result;
        if (!Take(',')) Fail("JSON array delimiter is invalid");
        Skip();
      }
    }
    if (Take('{')) {
      result.kind = JsonKind::kObject;
      Skip();
      if (Take('}')) return result;
      while (true) {
        if (position_ >= input_.size() || input_[position_] != '"')
          Fail("JSON object key is not a string");
        const std::string key = String();
        Skip();
        if (!Take(':')) Fail("JSON object key lacks colon");
        Skip();
        JsonValue value = Value(depth + 1U);
        if (!result.object.emplace(key, std::move(value)).second)
          Fail("JSON object contains duplicate key");
        Skip();
        if (Take('}')) return result;
        if (!Take(',')) Fail("JSON object delimiter is invalid");
        Skip();
      }
    }
    Fail("JSON token is invalid");
  }

  const std::string &input_;
  std::size_t position_ = 0U;
  std::uint64_t nodes_ = 0U;
};

JsonValue parse_json(const std::vector<std::uint8_t> &bytes) {
  return StrictJsonParser(std::string(bytes.begin(), bytes.end())).Parse();
}

const std::map<std::string, JsonValue> &as_object(const JsonValue &value,
                                                  const char *label) {
  if (value.kind != JsonKind::kObject) {
    throw AssembleError(std::string(label) + " is not an object");
  }
  return value.object;
}

const JsonValue &required(const std::map<std::string, JsonValue> &object,
                          const std::string &key) {
  const auto found = object.find(key);
  if (found == object.end()) {
    throw AssembleError("JSON object lacks required key: " + key);
  }
  return found->second;
}

void exact_keys(const std::map<std::string, JsonValue> &object,
                const std::vector<std::string> &keys, const char *label) {
  std::set<std::string> expected(keys.begin(), keys.end());
  if (object.size() != expected.size()) {
    throw AssembleError(std::string(label) + " key count differs");
  }
  for (const auto &entry : object) {
    if (expected.erase(entry.first) != 1U) {
      throw AssembleError(std::string(label) + " contains an unknown key");
    }
  }
}

std::uint64_t as_u64(const JsonValue &value, const char *label) {
  if (value.kind != JsonKind::kU64) {
    throw AssembleError(std::string(label) + " is not u64");
  }
  return value.u64;
}

bool as_bool(const JsonValue &value, const char *label) {
  if (value.kind != JsonKind::kBool) {
    throw AssembleError(std::string(label) + " is not Boolean");
  }
  return value.boolean;
}

const std::string &as_string(const JsonValue &value, const char *label) {
  if (value.kind != JsonKind::kString) {
    throw AssembleError(std::string(label) + " is not a string");
  }
  return value.string;
}

const std::vector<JsonValue> &as_array(const JsonValue &value,
                                       const char *label) {
  if (value.kind != JsonKind::kArray) {
    throw AssembleError(std::string(label) + " is not an array");
  }
  return value.array;
}

struct RunSpec {
  std::uint64_t sequence_index = 0U;
  std::string sequence_id;
  std::string bag_sha256;
  std::string resolved_parameters_sha256;
  std::string journal;
};

struct AssemblySpec {
  std::string config_sha256;
  std::string serial_pairs;
  std::string serial_pairs_sha256;
  std::array<RunSpec, 3> runs;
};

AssemblySpec parse_spec(const FileBytes &bytes) {
  const JsonValue parsed = parse_json(bytes.bytes);
  const auto &root = as_object(parsed, "assembly spec");
  exact_keys(root, {"schema_version", "record_type", "config_sha256",
                    "serial_pairs", "serial_pairs_sha256", "runs"},
             "assembly spec");
  if (as_u64(required(root, "schema_version"), "spec schema version") != 1U ||
      as_string(required(root, "record_type"), "spec record type") !=
          "cp2_recorded_assembly_spec") {
    throw AssembleError("assembly spec identity is invalid");
  }
  AssemblySpec result;
  result.config_sha256 =
      as_string(required(root, "config_sha256"), "configuration SHA-256");
  result.serial_pairs =
      as_string(required(root, "serial_pairs"), "serial-pair path");
  result.serial_pairs_sha256 = as_string(
      required(root, "serial_pairs_sha256"), "serial-pair SHA-256");
  if (!valid_sha256(result.config_sha256) ||
      !valid_sha256(result.serial_pairs_sha256) ||
      !normalized_absolute_path(result.serial_pairs)) {
    throw AssembleError("assembly spec has an invalid hash or serial path");
  }
  const std::vector<JsonValue> &runs =
      as_array(required(root, "runs"), "assembly runs");
  if (runs.size() != result.runs.size()) {
    throw AssembleError("assembly spec must contain exactly three runs");
  }
  std::set<std::string> paths;
  paths.insert(result.serial_pairs);
  for (std::size_t index = 0U; index < runs.size(); ++index) {
    const auto &row = as_object(runs[index], "assembly run");
    exact_keys(row, {"sequence_index", "sequence_id", "bag_sha256",
                     "resolved_parameters_sha256", "journal"},
               "assembly run");
    RunSpec &run = result.runs[index];
    run.sequence_index =
        as_u64(required(row, "sequence_index"), "run sequence index");
    run.sequence_id = as_string(required(row, "sequence_id"), "run sequence ID");
    run.bag_sha256 = as_string(required(row, "bag_sha256"), "bag SHA-256");
    run.resolved_parameters_sha256 = as_string(
        required(row, "resolved_parameters_sha256"), "parameter SHA-256");
    run.journal = as_string(required(row, "journal"), "journal path");
    if (run.sequence_index != index || run.sequence_id != kSequenceIds[index] ||
        !valid_sha256(run.bag_sha256) ||
        !valid_sha256(run.resolved_parameters_sha256) ||
        !normalized_absolute_path(run.journal) ||
        !paths.insert(run.journal).second) {
      throw AssembleError("assembly run identity, hash, or path is invalid");
    }
  }
  return result;
}

using SerialOwnerKey = std::pair<std::uint64_t, std::uint64_t>;
struct SerialOwner {
  std::uint64_t pair_index = 0U;
  std::uint64_t camera_timestamp_ns = 0U;
};

const std::vector<std::string> &serial_keys() {
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

std::map<SerialOwnerKey, SerialOwner>
parse_serial_pairs(const FileBytes &file) {
  const std::string text(file.bytes.begin(), file.bytes.end());
  if (!text.empty() && text.back() != '\n') {
    throw AssembleError("serial JSONL lacks its final newline");
  }
  std::map<SerialOwnerKey, SerialOwner> owners;
  std::array<std::uint64_t, 3> next_pairs{{0U, 0U, 0U}};
  std::array<std::uint64_t, 3> next_invocations{{0U, 0U, 0U}};
  std::pair<std::uint64_t, std::uint64_t> previous{0U, 0U};
  bool have_previous = false;
  std::size_t begin = 0U;
  while (begin < text.size()) {
    const std::size_t end = text.find('\n', begin);
    if (end == begin || end == std::string::npos) {
      throw AssembleError("serial JSONL contains an empty or unterminated row");
    }
    const JsonValue parsed = StrictJsonParser(text.substr(begin, end - begin)).Parse();
    const auto &row = as_object(parsed, "serial row");
    exact_keys(row, serial_keys(), "serial row");
    if (as_u64(required(row, "schema_version"), "serial schema") != 1U ||
        as_string(required(row, "record_type"), "serial type") !=
            "serial_pair") {
      throw AssembleError("serial row identity is invalid");
    }
    const std::uint64_t sequence =
        as_u64(required(row, "sequence_index"), "serial sequence");
    const std::uint64_t pair =
        as_u64(required(row, "pair_index"), "serial pair");
    if (sequence >= kSequenceIds.size() ||
        as_string(required(row, "sequence_id"), "serial sequence ID") !=
            kSequenceIds[sequence] || pair != next_pairs[sequence]) {
      throw AssembleError("serial sequence or contiguous pair identity is invalid");
    }
    next_pairs[sequence] = add_or_fail(pair, 1U, "serial pair index");
    const std::pair<std::uint64_t, std::uint64_t> order{sequence, pair};
    if (have_previous && !(previous < order)) {
      throw AssembleError("serial rows are not globally ordered");
    }
    previous = order;
    have_previous = true;
    const std::uint64_t anchor_camera =
        as_u64(required(row, "anchor_camera_id"), "anchor camera ID");
    const std::uint64_t cam0_record =
        as_u64(required(row, "cam0_record_time_ns"), "cam0 record time");
    const std::uint64_t cam1_record =
        as_u64(required(row, "cam1_record_time_ns"), "cam1 record time");
    const std::uint64_t cam0_header =
        as_u64(required(row, "cam0_header_time_ns"), "cam0 header time");
    const std::uint64_t camera_timestamp =
        as_u64(required(row, "camera_timestamp_ns"), "camera timestamp");
    const std::uint64_t delta = cam0_record > cam1_record
                                    ? cam0_record - cam1_record
                                    : cam1_record - cam0_record;
    for (const char *field : {"anchor_filtered_index", "cam0_filtered_index",
                              "cam1_filtered_index", "cam1_header_time_ns"}) {
      (void)as_u64(required(row, field), field);
    }
    if (anchor_camera > 1U || camera_timestamp != cam0_header ||
        as_u64(required(row, "absolute_record_delta_ns"), "record delta") !=
            delta || delta >= UINT64_C(20000000) ||
        !as_bool(required(row, "selected"), "serial selected")) {
      throw AssembleError("serial timing/selection invariant is invalid");
    }
    const bool enqueue_entered =
        as_bool(required(row, "enqueue_entered"), "enqueue entered");
    const bool enqueue_returned =
        as_bool(required(row, "enqueue_returned"), "enqueue returned");
    const bool processing_entered =
        as_bool(required(row, "processing_entered"), "processing entered");
    const bool processing_returned =
        as_bool(required(row, "processing_returned"), "processing returned");
    const std::string &enqueue =
        as_string(required(row, "enqueue_status"), "enqueue status");
    const std::string &processing =
        as_string(required(row, "processing_status"), "processing status");
    const bool processed = enqueue == "queued" && processing == "processed" &&
                           enqueue_entered && enqueue_returned &&
                           processing_entered && processing_returned;
    const bool dropped = enqueue == "frequency_dropped" &&
                         processing == "not_queued" && enqueue_entered &&
                         enqueue_returned && !processing_entered &&
                         !processing_returned;
    if (!processed && !dropped) {
      throw AssembleError("serial status transition is not a passing transition");
    }
    const std::vector<JsonValue> &ids = as_array(
        required(row, "updater_invocation_ids"), "updater invocation IDs");
    if (!processed && !ids.empty()) {
      throw AssembleError("unprocessed serial row owns updater invocations");
    }
    std::set<std::uint64_t> local;
    for (const JsonValue &value : ids) {
      const std::uint64_t invocation = as_u64(value, "updater invocation ID");
      if (invocation != next_invocations[sequence] ||
          !local.insert(invocation).second ||
          !owners.emplace(SerialOwnerKey{sequence, invocation},
                          SerialOwner{pair, camera_timestamp}).second) {
        throw AssembleError("serial updater ownership is duplicate");
      }
      next_invocations[sequence] = add_or_fail(
          next_invocations[sequence], 1U, "serial invocation ID");
    }
    begin = end + 1U;
  }
  return owners;
}

std::string json_quote(const std::string &value) {
  if (!valid_utf8(value)) {
    throw AssembleError("attempted to emit a non-UTF-8 JSON string");
  }
  std::string output = "\"";
  static const char hex[] = "0123456789abcdef";
  for (unsigned char byte : value) {
    switch (byte) {
    case '"': output += "\\\""; break;
    case '\\': output += "\\\\"; break;
    case '\b': output += "\\b"; break;
    case '\f': output += "\\f"; break;
    case '\n': output += "\\n"; break;
    case '\r': output += "\\r"; break;
    case '\t': output += "\\t"; break;
    default:
      if (byte < UINT8_C(0x20)) {
        output += "\\u00";
        output.push_back(hex[byte >> 4U]);
        output.push_back(hex[byte & UINT8_C(0x0f)]);
      } else {
        output.push_back(static_cast<char>(byte));
      }
    }
  }
  output.push_back('"');
  return output;
}

std::string json_u64(std::uint64_t value) { return std::to_string(value); }
std::string json_i64(std::int64_t value) { return std::to_string(value); }
std::string json_bool(bool value) { return value ? "true" : "false"; }

std::string json_f64(double value) {
  if (!std::isfinite(value)) {
    throw AssembleError("attempted to emit nonfinite JSON number");
  }
  std::ostringstream output;
  output.imbue(std::locale::classic());
  output << std::setprecision(std::numeric_limits<double>::max_digits10)
         << value;
  std::string result = output.str();
  if (result.find_first_of(".eE") == std::string::npos) {
    result += ".0";
  }
  return result;
}

class JsonObjectWriter {
public:
  JsonObjectWriter() : output_("{") {}
  void Add(const std::string &key, const std::string &raw_value) {
    if (!first_) output_.push_back(',');
    first_ = false;
    output_ += json_quote(key);
    output_.push_back(':');
    output_ += raw_value;
  }
  std::string Finish() {
    output_.push_back('}');
    return std::move(output_);
  }

private:
  bool first_ = true;
  std::string output_;
};

template <typename Iterator, typename Encoder>
std::string json_array(Iterator begin, Iterator end, Encoder encoder) {
  std::string result = "[";
  bool first = true;
  for (; begin != end; ++begin) {
    if (!first) result.push_back(',');
    first = false;
    result += encoder(*begin);
  }
  result.push_back(']');
  return result;
}

std::string json_u64_array(const std::vector<std::uint64_t> &values) {
  return json_array(values.begin(), values.end(),
                    [](std::uint64_t value) { return json_u64(value); });
}

std::string nullable_string(bool available, const std::string &value) {
  return available ? json_quote(value) : "null";
}
std::string nullable_u64(bool available, std::uint64_t value) {
  return available ? json_u64(value) : "null";
}
std::string nullable_f64(bool available, double value) {
  return available ? json_f64(value) : "null";
}
std::string nullable_bool(bool available, bool value) {
  return available ? json_bool(value) : "null";
}

void write_all(int descriptor, const std::uint8_t *data, std::size_t size) {
  std::size_t offset = 0U;
  while (offset < size) {
    ssize_t amount = -1;
    do {
      amount = ::write(descriptor, data + offset, size - offset);
    } while (amount < 0 && errno == EINTR);
    if (amount <= 0) {
      throw AssembleError("output write failed");
    }
    offset += static_cast<std::size_t>(amount);
  }
}

void write_new_file(int directory, const char *name,
                    const std::vector<std::uint8_t> &bytes) {
  OwnedDescriptor output(::openat(directory, name,
                                  O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW |
                                      O_CLOEXEC,
                                  0600));
  if (!output.valid()) {
    throw AssembleError(std::string("cannot create output: ") + name);
  }
  write_all(output.get(), bytes.data(), bytes.size());
  if (::fsync(output.get()) != 0) {
    throw AssembleError(std::string("cannot sync output: ") + name);
  }
  const FileIdentity identity = file_identity(output.get());
  if (identity.size < 0 || static_cast<std::uint64_t>(identity.size) !=
                               static_cast<std::uint64_t>(bytes.size())) {
    throw AssembleError(std::string("output size differs: ") + name);
  }
}

void write_new_file(int directory, const char *name, const std::string &bytes) {
  write_new_file(directory, name,
                 std::vector<std::uint8_t>(bytes.begin(), bytes.end()));
}

void require_empty_directory(int descriptor) {
  const int duplicate = ::fcntl(descriptor, F_DUPFD_CLOEXEC, 3);
  if (duplicate < 0) throw AssembleError("cannot inspect output directory");
  DIR *stream = ::fdopendir(duplicate);
  if (stream == nullptr) {
    (void)::close(duplicate);
    throw AssembleError("cannot inspect output directory");
  }
  errno = 0;
  while (dirent *entry = ::readdir(stream)) {
    const std::string name(entry->d_name);
    if (name != "." && name != "..") {
      (void)::closedir(stream);
      throw AssembleError("output directory is not empty");
    }
    errno = 0;
  }
  const int saved = errno;
  (void)::closedir(stream);
  if (saved != 0) throw AssembleError("cannot enumerate output directory");
}

using InvocationKey =
    std::tuple<std::uint64_t, std::uint64_t, std::uint64_t>;
using StateKey =
    std::tuple<std::uint64_t, std::uint64_t, std::uint64_t, std::uint64_t>;
using ProposalKey =
    std::tuple<std::uint64_t, std::uint64_t, std::uint64_t, std::uint64_t>;
using RawKey = std::tuple<std::uint64_t, std::uint64_t, std::uint64_t,
                          std::uint64_t, std::uint64_t>;

InvocationKey invocation_key(
    const ov_msckf::CP2InvocationTraceIdentity &identity) {
  return {identity.sequence_index, identity.pair_index, identity.invocation_id};
}

struct AggregateCounters {
  std::uint64_t jitter = 0U;
  std::uint64_t repair = 0U;
  std::uint64_t alternate_solve = 0U;
  std::uint64_t clamp = 0U;
  std::uint64_t regularization = 0U;
  std::uint64_t silent_fallback = 0U;
  std::uint64_t fallback = 0U;
};

void add_counters(AggregateCounters &total,
                  const ov_msckf::CP2TraceJournalCounterSummary &value) {
  total.jitter = add_or_fail(total.jitter, value.jitter, "jitter total");
  total.repair = add_or_fail(total.repair, value.repair, "repair total");
  total.alternate_solve = add_or_fail(total.alternate_solve,
                                      value.alternate_solve,
                                      "alternate-solve total");
  total.clamp = add_or_fail(total.clamp, value.clamp, "clamp total");
  total.regularization = add_or_fail(total.regularization,
                                     value.regularization,
                                     "regularization total");
  total.silent_fallback = add_or_fail(total.silent_fallback,
                                      value.silent_fallback,
                                      "silent-fallback total");
  total.fallback = add_or_fail(total.fallback, value.fallback,
                               "fallback total");
}

bool counters_zero(const ov_msckf::CP2TraceJournalCounterSummary &value) {
  return value.jitter == 0U && value.repair == 0U &&
         value.alternate_solve == 0U && value.clamp == 0U &&
         value.regularization == 0U && value.silent_fallback == 0U &&
         value.fallback == 0U;
}

bool counters_zero(const AggregateCounters &value) {
  return value.jitter == 0U && value.repair == 0U &&
         value.alternate_solve == 0U && value.clamp == 0U &&
         value.regularization == 0U && value.silent_fallback == 0U &&
         value.fallback == 0U;
}

void require_tonearest(const char *label) {
  if (std::fegetround() != FE_TONEAREST)
    throw AssembleError(std::string(label) + " requires FE_TONEAREST");
}

std::string counter_json(
    const ov_msckf::CP2TraceJournalCounterSummary &value) {
  JsonObjectWriter row;
  row.Add("jitter", json_u64(value.jitter));
  row.Add("repair", json_u64(value.repair));
  row.Add("alternate_solve", json_u64(value.alternate_solve));
  row.Add("clamp", json_u64(value.clamp));
  row.Add("regularization", json_u64(value.regularization));
  row.Add("silent_fallback", json_u64(value.silent_fallback));
  row.Add("fallback", json_u64(value.fallback));
  return row.Finish();
}

std::string counter_json(const AggregateCounters &value) {
  JsonObjectWriter row;
  row.Add("jitter", json_u64(value.jitter));
  row.Add("repair", json_u64(value.repair));
  row.Add("alternate_solve", json_u64(value.alternate_solve));
  row.Add("clamp", json_u64(value.clamp));
  row.Add("regularization", json_u64(value.regularization));
  row.Add("silent_fallback", json_u64(value.silent_fallback));
  row.Add("fallback", json_u64(value.fallback));
  return row.Finish();
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

template <typename Reference, typename Candidate>
BlockComparison compare_block(const Eigen::MatrixBase<Reference> &reference,
                              bool candidate_available,
                              const Eigen::MatrixBase<Candidate> &candidate) {
  BlockComparison result;
  result.candidate_available = candidate_available;
  if (!candidate_available) return result;
  result.reference_norm = reference.norm();
  if (!std::isfinite(result.reference_norm) || result.reference_norm < 0.0) {
    result.status = "reference_norm_nonfinite";
    return result;
  }
  const typename Reference::PlainObject difference = candidate - reference;
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
  require_tonearest("block comparison ratio");
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

const char *semantic_kind_name(ov_msckf::CP2SemanticStateKind kind) {
  static const std::array<const char *, 8> names{{
      "imu.theta", "imu.position", "imu.velocity", "imu.gyro_bias",
      "imu.accel_bias", "clone.theta", "clone.position", "slam_landmark"}};
  const std::size_t index = static_cast<std::size_t>(kind);
  if (index >= names.size()) throw AssembleError("semantic block kind is invalid");
  return names[index];
}

const char *landmark_representation_name(
    ov_msckf::CP2LandmarkRepresentation value) {
  static const std::array<const char *, 6> names{{
      "GLOBAL_3D", "GLOBAL_FULL_INVERSE_DEPTH", "ANCHORED_3D",
      "ANCHORED_FULL_INVERSE_DEPTH", "ANCHORED_MSCKF_INVERSE_DEPTH",
      "ANCHORED_INVERSE_DEPTH_SINGLE"}};
  const std::size_t index = static_cast<std::size_t>(value);
  if (index >= names.size())
    throw AssembleError("landmark representation is invalid");
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

std::string block_identity_json(const ov_msckf::CP2SemanticStateBlock &block) {
  JsonObjectWriter identity;
  identity.Add("kind", json_quote(semantic_kind_name(block.kind)));
  if (block.kind == ov_msckf::CP2SemanticStateKind::kCloneTheta ||
      block.kind == ov_msckf::CP2SemanticStateKind::kClonePosition) {
    identity.Add("timestamp_bits", json_quote(binary64_hex(block.clone_timestamp)));
  } else if (block.kind == ov_msckf::CP2SemanticStateKind::kSlamLandmark) {
    identity.Add("feature_id", json_u64(block.landmark.feature_id));
    identity.Add("representation",
                 json_quote(landmark_representation_name(
                     block.landmark.representation)));
    identity.Add("anchor_camera_id", json_i64(block.landmark.anchor_camera_id));
    identity.Add("anchor_timestamp_bits",
                 json_quote(binary64_hex(block.landmark.anchor_timestamp)));
  }
  return identity.Finish();
}

std::string proposal_outcome(
    const ov_msckf::CP2TraceJournalDecodedEvent &event) {
  if (!event.core.shadow_evidence_available) return "not_reached";
  if (!event.core.schur_assembly_valid) return "internal_failure";
  if (event.global.schur_gamma_status == ov_msckf::CP2GammaStatus::kNonfinite)
    return "gamma_nonfinite";
  if (event.candidate_accepted_ids.empty() ||
      event.global.schur_precompression_rows < 1U)
    return "all_rejected";
  if (event.global.schur_compressed_rows < 1U) return "empty_after_compression";
  return event.core.schur_proposal_available ? "proposal_available"
                                              : "preflight_rejected";
}

bool same_binary64(double left, double right) noexcept;

bool statistic_valid(
    bool required,
    const ov_msckf::CP2TraceJournalStatisticSummary &value) {
  if (!required) {
    return value.status == ov_msckf::CP2StatisticComparisonStatus::kNotRequired &&
           !value.available && !value.passed;
  }
  if (value.status != ov_msckf::CP2StatisticComparisonStatus::kAvailable ||
      !value.available || !std::isfinite(value.reference_norm) ||
      !std::isfinite(value.error) || value.reference_norm < 0.0 ||
      value.error < 0.0) {
    return false;
  }
  const double scaled = 1.0e-8 * value.reference_norm;
  const double tolerance = 1.0e-10 + scaled;
  if (!std::isfinite(scaled) || !std::isfinite(tolerance) ||
      tolerance < 0.0) {
    return false;
  }
  require_tonearest("statistic comparison ratio");
  const double ratio = value.error / tolerance;
  return std::isfinite(ratio) && ratio >= 0.0 &&
         same_binary64(value.tolerance, tolerance) &&
         same_binary64(value.ratio, ratio) &&
         value.passed && value.error <= tolerance;
}

struct EventContext {
  const AssemblySpec *spec = nullptr;
  const RunSpec *run = nullptr;
  const ov_msckf::CP2TraceJournalDecodedEvent *event = nullptr;
  std::array<const ov_msckf::CP2StateTraceFrame *, 4> states{{
      nullptr, nullptr, nullptr, nullptr}};
  const ov_msckf::CP2ProposalTraceFrame *baseline_proposal = nullptr;
  const ov_msckf::CP2ProposalTraceFrame *candidate_proposal = nullptr;
  std::vector<const ov_msckf::CP2RawSystemTraceFrame *> raw_frames;
  ov_msckf::CP2AcceptedFeatureDigests baseline_digests;
  ov_msckf::CP2AcceptedFeatureDigests candidate_digests;
  bool commit_exact = false;
  bool feature_math_passed = true;
  bool blocks_passed = true;
  bool all_block_rows_present = true;
  bool math_passed = false;
  std::uint64_t state_block_rows = 0U;
  std::uint64_t covariance_block_rows = 0U;
};

std::string payload_sha(const ov_msckf::CP2TracePayloadReference *reference) {
  return reference == nullptr ? "null" : json_quote(reference->sha256);
}
std::string payload_offset(const ov_msckf::CP2TracePayloadReference *reference) {
  return reference == nullptr ? "null" : json_u64(reference->offset);
}
std::string payload_length(const ov_msckf::CP2TracePayloadReference *reference) {
  return reference == nullptr ? "null" : json_u64(reference->length);
}

void add_identity(JsonObjectWriter &row, const EventContext &context) {
  const auto &event = *context.event;
  row.Add("sequence_index", json_u64(event.sequence_index));
  row.Add("pair_index", json_u64(event.pair_index));
  row.Add("camera_timestamp_ns", json_u64(event.camera_timestamp_ns));
  row.Add("invocation_id", json_u64(event.invocation_id));
}

void add_repeated(JsonObjectWriter &row, const EventContext &context) {
  const auto &event = *context.event;
  const bool baseline_gamma =
      event.global.nullspace_gamma_status == ov_msckf::CP2GammaStatus::kAvailable;
  const bool candidate_gamma =
      event.global.schur_gamma_status == ov_msckf::CP2GammaStatus::kAvailable;
  row.Add("prior_snapshot_sha256",
          context.states[0] == nullptr ? "null"
                                       : json_quote(context.states[0]->payload.sha256));
  row.Add("config_sha256", json_quote(context.spec->config_sha256));
  row.Add("bag_sha256", json_quote(context.run->bag_sha256));
  row.Add("pair_index_sha256",
          json_quote(context.spec->serial_pairs_sha256));
  row.Add("resolved_parameters_sha256",
          json_quote(context.run->resolved_parameters_sha256));
  row.Add("baseline_accepted_set_sha256",
          json_quote(context.baseline_digests.set_sha256));
  row.Add("baseline_accepted_sequence_sha256",
          json_quote(context.baseline_digests.sequence_sha256));
  row.Add("candidate_accepted_set_sha256",
          json_quote(context.candidate_digests.set_sha256));
  row.Add("candidate_accepted_sequence_sha256",
          json_quote(context.candidate_digests.sequence_sha256));
  row.Add("baseline_retained_gamma",
          nullable_f64(baseline_gamma,
                       event.global.nullspace_retained_gamma));
  row.Add("candidate_retained_gamma",
          nullable_f64(candidate_gamma, event.global.schur_retained_gamma));
}

std::uint64_t eigen_u64(Eigen::Index value, const char *label) {
  std::uint64_t result = 0U;
  if (!ov_msckf::cp2_checked_eigen_index_to_u64(value, result)) {
    throw AssembleError(std::string(label) + " is outside u64");
  }
  return result;
}

std::string layout_json(
    const std::vector<ov_msckf::CP2FeatureGateLayoutBlock> &layout) {
  return json_array(layout.begin(), layout.end(), [](const auto &block) {
    JsonObjectWriter entry;
    entry.Add("covariance_id", json_u64(eigen_u64(block.covariance_id,
                                                   "layout covariance ID")));
    entry.Add("size", json_u64(eigen_u64(block.size, "layout size")));
    return entry.Finish();
  });
}

void add_statistic(JsonObjectWriter &row, const char *stem,
                   const ov_msckf::CP2TraceJournalStatisticSummary &value,
                   bool status_only = false) {
  const std::string prefix(stem);
  if (!status_only) {
    row.Add(prefix + "_comparison_available", json_bool(value.available));
  } else {
    row.Add(prefix + "_comparison_status",
            json_quote(ov_msckf::cp2_statistic_comparison_status_name(
                value.status)));
  }
}

void add_statistic_values(
    JsonObjectWriter &row, const char *stem,
    const ov_msckf::CP2TraceJournalStatisticSummary &value) {
  const std::string prefix(stem);
  row.Add(prefix + "_reference_norm",
          nullable_f64(value.available, value.reference_norm));
  row.Add(prefix + "_error", nullable_f64(value.available, value.error));
  row.Add(prefix + "_tolerance",
          nullable_f64(value.available, value.tolerance));
  row.Add(prefix + "_ratio", nullable_f64(value.available, value.ratio));
  row.Add(prefix + "_pass", json_bool(value.passed));
}

std::string feature_row_json(
    const EventContext &context, std::size_t ordinal,
    const ov_msckf::CP2RawSystemTraceFrame &raw,
    const ov_msckf::CP2TraceJournalFeatureSummary &feature) {
  const std::uint64_t raw_rows = eigen_u64(raw.raw_system.H_x.rows(), "raw rows");
  if (feature.feature_id != raw.raw_system.feature_id ||
      feature.raw_rows != raw_rows || feature.nullspace_raw_rows != raw_rows ||
      feature.schur_raw_rows != raw_rows || context.states[0] == nullptr) {
    throw AssembleError("feature summary differs from its raw system/prior");
  }
  JsonObjectWriter row;
  row.Add("schema_version", "1");
  row.Add("record_type", json_quote("feature_comparison"));
  add_identity(row, context);
  row.Add("feature_ordinal", json_u64(static_cast<std::uint64_t>(ordinal)));
  row.Add("feature_id", json_u64(feature.feature_id));
  row.Add("pass_index", "1");
  row.Add("raw_rows", json_u64(raw_rows));
  row.Add("raw_system_sha256", json_quote(raw.payload.sha256));
  row.Add("jacobian_layout", layout_json(raw.raw_system.jacobian_layout));
  row.Add("raw_payload_offset", json_u64(raw.payload.offset));
  row.Add("raw_payload_length", json_u64(raw.payload.length));
  row.Add("prior_snapshot_sha256",
          json_quote(context.states[0]->payload.sha256));
  row.Add("config_sha256", json_quote(context.spec->config_sha256));
  row.Add("bag_sha256", json_quote(context.run->bag_sha256));
  row.Add("pair_index_sha256", json_quote(context.spec->serial_pairs_sha256));
  row.Add("resolved_parameters_sha256",
          json_quote(context.run->resolved_parameters_sha256));
  row.Add("baseline_accepted_set_sha256",
          json_quote(context.baseline_digests.set_sha256));
  row.Add("baseline_accepted_sequence_sha256",
          json_quote(context.baseline_digests.sequence_sha256));
  row.Add("candidate_accepted_set_sha256",
          json_quote(context.candidate_digests.set_sha256));
  row.Add("candidate_accepted_sequence_sha256",
          json_quote(context.candidate_digests.sequence_sha256));
  const bool baseline_gamma = context.event->global.nullspace_gamma_status ==
                              ov_msckf::CP2GammaStatus::kAvailable;
  const bool candidate_gamma = context.event->global.schur_gamma_status ==
                               ov_msckf::CP2GammaStatus::kAvailable;
  row.Add("baseline_retained_gamma",
          nullable_f64(baseline_gamma,
                       context.event->global.nullspace_retained_gamma));
  row.Add("candidate_retained_gamma",
          nullable_f64(candidate_gamma,
                       context.event->global.schur_retained_gamma));
  row.Add("nullspace_reduction_status",
          json_quote(ov_msckf::cp2_nullspace_reduction_status_name(
              feature.nullspace_status)));
  row.Add("nullspace_reduction_stage",
          json_quote(ov_msckf::cp2_nullspace_reduction_stage_name(
              feature.nullspace_stage)));
  row.Add("nullspace_reduced_rows", json_u64(feature.nullspace_reduced_rows));
  row.Add("nullspace_raw_lambda_symmetry_error_inf",
          nullable_f64(feature.nullspace_raw_lambda_symmetry_error_available,
                       feature.nullspace_raw_lambda_symmetry_error_inf));
  row.Add("nullspace_gamma",
          nullable_f64(feature.nullspace_mode_gamma_available,
                       feature.nullspace_mode_gamma));
  row.Add("nullspace_nis",
          nullable_f64(feature.nullspace_gate.chi2_available,
                       feature.nullspace_gate.chi2));
  row.Add("nullspace_threshold",
          nullable_f64(feature.nullspace_gate.threshold_available,
                       feature.nullspace_gate.threshold));
  row.Add("nullspace_gate_stage",
          json_quote(ov_msckf::cp2_feature_gate_stage_name(
              feature.nullspace_gate.stage)));
  row.Add("nullspace_decision",
          nullable_bool(feature.nullspace_gate.evidence_decision_available,
                        feature.nullspace_gate.evidence_accept));
  row.Add("schur_reduction_status",
          json_quote(ov_msckf::schur_reduction_status_name(feature.schur_status)));
  row.Add("schur_reduction_stage",
          json_quote(ov_msckf::schur_reduction_stage_name(feature.schur_stage)));
  row.Add("schur_reduced_rows", json_u64(feature.schur_degrees_of_freedom));
  row.Add("schur_singular_values_available",
          json_bool(feature.schur_singular_values_available));
  row.Add("schur_singular_values",
          feature.schur_singular_values_available
              ? json_array(feature.schur_singular_values.begin(),
                           feature.schur_singular_values.end(),
                           [](double value) { return json_f64(value); })
              : "null");
  row.Add("schur_ratio_available", json_bool(feature.schur_ratio_available));
  row.Add("schur_ratio",
          nullable_f64(feature.schur_ratio_available, feature.schur_ratio));
  const bool schur_accepted =
      feature.schur_status == ov_msckf::SchurReductionStatus::kAccepted;
  row.Add("schur_raw_lambda_symmetry_error_inf",
          nullable_f64(schur_accepted,
                       feature.schur_raw_lambda_symmetry_error_inf));
  row.Add("schur_gamma", nullable_f64(schur_accepted, feature.schur_gamma));
  row.Add("schur_nis", nullable_f64(feature.schur_gate.chi2_available,
                                     feature.schur_gate.chi2));
  row.Add("schur_threshold",
          nullable_f64(feature.schur_gate.threshold_available,
                       feature.schur_gate.threshold));
  row.Add("schur_gate_stage",
          json_quote(ov_msckf::cp2_feature_gate_stage_name(
              feature.schur_gate.stage)));
  row.Add("schur_decision",
          nullable_bool(feature.schur_gate.evidence_decision_available,
                        feature.schur_gate.evidence_accept));
  row.Add("statistics_comparison_required",
          json_bool(feature.statistics_comparison_required));
  add_statistic(row, "lambda", feature.lambda_comparison);
  add_statistic(row, "eta", feature.eta_comparison);
  add_statistic(row, "gamma", feature.gamma_comparison);
  add_statistic(row, "lambda", feature.lambda_comparison, true);
  add_statistic(row, "eta", feature.eta_comparison, true);
  add_statistic(row, "gamma", feature.gamma_comparison, true);
  add_statistic_values(row, "lambda", feature.lambda_comparison);
  add_statistic_values(row, "eta", feature.eta_comparison);
  add_statistic_values(row, "gamma", feature.gamma_comparison);
  row.Add("agreement_class",
          json_quote(ov_msckf::cp2_gate_agreement_class_name(
              feature.agreement_class)));
  row.Add("raw_row_match_weight", json_u64(feature.raw_row_match_weight));
  row.Add("nullspace_reducer_counters",
          counter_json(feature.nullspace_reducer_counters));
  row.Add("schur_reducer_counters",
          counter_json(feature.schur_reducer_counters));
  return row.Finish();
}

void add_block_diagnostics(JsonObjectWriter &row,
                           const BlockComparison &comparison) {
  row.Add("candidate_available", json_bool(comparison.candidate_available));
  row.Add("comparison_available", json_bool(comparison.comparison_available));
  row.Add("comparison_status", json_quote(comparison.status));
  row.Add("reference_norm",
          nullable_f64(comparison.comparison_available,
                       comparison.reference_norm));
  row.Add("error",
          nullable_f64(comparison.comparison_available, comparison.error));
  row.Add("tolerance",
          nullable_f64(comparison.comparison_available, comparison.tolerance));
  row.Add("ratio",
          nullable_f64(comparison.comparison_available, comparison.ratio));
}

std::string state_block_row_json(
    const EventContext &context, std::size_t index,
    const ov_msckf::CP2SemanticStateBlock &block,
    const BlockComparison &comparison) {
  JsonObjectWriter row;
  row.Add("schema_version", "1");
  row.Add("record_type", json_quote("state_block_comparison"));
  add_identity(row, context);
  row.Add("block_index", json_u64(static_cast<std::uint64_t>(index)));
  row.Add("block_kind", json_quote(semantic_kind_name(block.kind)));
  row.Add("block_identity", block_identity_json(block));
  row.Add("covariance_id", json_u64(block.covariance_id));
  row.Add("size", json_u64(block.error_size));
  add_block_diagnostics(row, comparison);
  add_repeated(row, context);
  row.Add("passed", json_bool(comparison.passed));
  return row.Finish();
}

std::string covariance_block_row_json(
    const EventContext &context, std::size_t row_index,
    std::size_t column_index,
    const ov_msckf::CP2SemanticStateBlock &row_block,
    const ov_msckf::CP2SemanticStateBlock &column_block,
    const BlockComparison &comparison) {
  JsonObjectWriter row;
  row.Add("schema_version", "1");
  row.Add("record_type", json_quote("covariance_block_comparison"));
  add_identity(row, context);
  row.Add("row_block_index", json_u64(static_cast<std::uint64_t>(row_index)));
  row.Add("column_block_index",
          json_u64(static_cast<std::uint64_t>(column_index)));
  row.Add("row_covariance_id", json_u64(row_block.covariance_id));
  row.Add("column_covariance_id", json_u64(column_block.covariance_id));
  row.Add("row_size", json_u64(row_block.error_size));
  row.Add("column_size", json_u64(column_block.error_size));
  add_block_diagnostics(row, comparison);
  add_repeated(row, context);
  row.Add("passed", json_bool(comparison.passed));
  return row.Finish();
}

bool oracle_equal(const ov_msckf::CP2CommitOracleResult &left,
                  const ov_msckf::CP2CommitOracleResult &right) noexcept {
  return left.status == right.status &&
         left.comparison_available == right.comparison_available &&
         left.structure_equal == right.structure_equal &&
         left.phase0_valid == right.phase0_valid &&
         left.phase2_valid == right.phase2_valid &&
         left.phase3_valid == right.phase3_valid &&
         left.complete_finite == right.complete_finite &&
         left.phase0_phase2_immutable_equal ==
             right.phase0_phase2_immutable_equal &&
         left.phase2_phase3_canonical_equal ==
             right.phase2_phase3_canonical_equal &&
         left.complete_canonical_equal == right.complete_canonical_equal &&
         left.type_update_calls_match == right.type_update_calls_match &&
         left.passed == right.passed &&
         left.observed_type_update_calls == right.observed_type_update_calls &&
         left.baseline_expected_type_update_calls ==
             right.baseline_expected_type_update_calls &&
         left.baseline_verified_nominal_fields ==
             right.baseline_verified_nominal_fields &&
         left.baseline_nominal_mismatches == right.baseline_nominal_mismatches &&
         left.baseline_covariance_mismatches ==
             right.baseline_covariance_mismatches &&
         left.baseline_fej_mismatches == right.baseline_fej_mismatches &&
         left.state_blocks_expected == right.state_blocks_expected &&
         left.state_blocks_seen == right.state_blocks_seen &&
         left.covariance_blocks_expected == right.covariance_blocks_expected &&
         left.covariance_blocks_seen == right.covariance_blocks_seen;
}

std::string update_row_json(const EventContext &context) {
  const auto &event = *context.event;
  const auto &core = event.core;
  const bool raw = event.raw_system_count != 0U;
  const bool committed = core.terminal_status ==
                         ov_msckf::CP2UpdateTerminalStatus::kCommittedCounted;
  const ov_msckf::CP2TracePayloadReference *phase0 =
      context.states[0] == nullptr ? nullptr : &context.states[0]->payload;
  const ov_msckf::CP2TracePayloadReference *phase1 =
      context.states[1] == nullptr ? nullptr : &context.states[1]->payload;
  const ov_msckf::CP2TracePayloadReference *phase2 =
      context.states[2] == nullptr ? nullptr : &context.states[2]->payload;
  const ov_msckf::CP2TracePayloadReference *phase3 =
      context.states[3] == nullptr ? nullptr : &context.states[3]->payload;
  const ov_msckf::CP2TracePayloadReference *baseline =
      context.baseline_proposal == nullptr
          ? nullptr
          : &context.baseline_proposal->payload;
  const ov_msckf::CP2TracePayloadReference *candidate =
      context.candidate_proposal == nullptr
          ? nullptr
          : &context.candidate_proposal->payload;
  const bool candidate_compression_reached =
      core.shadow_math_completed &&
      event.global.schur_gamma_status == ov_msckf::CP2GammaStatus::kAvailable &&
      event.global.schur_precompression_rows > 0U;
  const bool candidate_preview_reached =
      candidate_compression_reached && event.global.schur_compressed_rows > 0U;

  JsonObjectWriter row;
  row.Add("schema_version", "1");
  row.Add("record_type", json_quote("updater_invocation"));
  row.Add("sequence_index", json_u64(event.sequence_index));
  row.Add("sequence_id", json_quote(context.run->sequence_id));
  row.Add("pair_index", json_u64(event.pair_index));
  row.Add("camera_timestamp_ns", json_u64(event.camera_timestamp_ns));
  row.Add("invocation_id", json_u64(event.invocation_id));
  row.Add("live_mode", json_quote("nullspace"));
  row.Add("shadow_mode", json_quote("schur"));
  row.Add("shadow_enabled", "true");
  row.Add("timing_evidence_eligible", "false");
  row.Add("duration_ns", json_u64(core.duration_ns));
  row.Add("terminal_status",
          json_quote(ov_msckf::cp2_update_terminal_status_name(
              core.terminal_status)));
  row.Add("terminal_subreason",
          json_quote(ov_msckf::cp2_update_terminal_subreason_name(
              core.terminal_subreason)));
  row.Add("input_feature_count", json_u64(core.input_feature_count));
  row.Add("raw_system_count", json_u64(event.raw_system_count));
  row.Add("prior_snapshot_sha256", payload_sha(phase0));
  row.Add("precommit_snapshot_sha256", payload_sha(phase1));
  row.Add("prior_payload_offset", payload_offset(phase0));
  row.Add("prior_payload_length", payload_length(phase0));
  row.Add("precommit_payload_offset", payload_offset(phase1));
  row.Add("precommit_payload_length", payload_length(phase1));
  row.Add("expected_postcommit_snapshot_sha256", payload_sha(phase2));
  row.Add("expected_postcommit_payload_offset", payload_offset(phase2));
  row.Add("expected_postcommit_payload_length", payload_length(phase2));
  row.Add("live_postcommit_snapshot_sha256", payload_sha(phase3));
  row.Add("live_postcommit_payload_offset", payload_offset(phase3));
  row.Add("live_postcommit_payload_length", payload_length(phase3));
  row.Add("baseline_proposal_sha256", payload_sha(baseline));
  row.Add("baseline_proposal_payload_offset", payload_offset(baseline));
  row.Add("baseline_proposal_payload_length", payload_length(baseline));
  row.Add("candidate_proposal_sha256", payload_sha(candidate));
  row.Add("candidate_proposal_payload_offset", payload_offset(candidate));
  row.Add("candidate_proposal_payload_length", payload_length(candidate));
  row.Add("zero_write_snapshot_equal",
          json_bool(!raw || (context.states[0] != nullptr &&
                             context.states[1] != nullptr &&
                             ov_msckf::CP2CompositeStateAdapter::CanonicallyEqual(
                                 context.states[0]->snapshot,
                                 context.states[1]->snapshot))));
  row.Add("baseline_accepted_ids", json_u64_array(event.baseline_accepted_ids));
  row.Add("baseline_accepted_set_sha256",
          json_quote(context.baseline_digests.set_sha256));
  row.Add("baseline_accepted_sequence_sha256",
          json_quote(context.baseline_digests.sequence_sha256));
  row.Add("candidate_accepted_ids", json_u64_array(event.candidate_accepted_ids));
  row.Add("candidate_accepted_set_sha256",
          json_quote(context.candidate_digests.set_sha256));
  row.Add("candidate_accepted_sequence_sha256",
          json_quote(context.candidate_digests.sequence_sha256));
  row.Add("baseline_gamma_status",
          json_quote(ov_msckf::cp2_gamma_status_name(
              event.global.nullspace_gamma_status)));
  row.Add("baseline_gamma",
          nullable_f64(event.global.nullspace_gamma_status ==
                           ov_msckf::CP2GammaStatus::kAvailable,
                       event.global.nullspace_retained_gamma));
  row.Add("candidate_gamma",
          nullable_f64(event.global.schur_gamma_status ==
                           ov_msckf::CP2GammaStatus::kAvailable,
                       event.global.schur_retained_gamma));
  row.Add("baseline_precompression_rows",
          nullable_u64(core.baseline_precompression_rows_available,
                       event.global.nullspace_precompression_rows));
  row.Add("baseline_compressed_rows",
          nullable_u64(core.baseline_compressed_rows_available,
                       event.global.nullspace_compressed_rows));
  row.Add("candidate_precompression_rows",
          nullable_u64(core.shadow_math_completed,
                       event.global.schur_precompression_rows));
  row.Add("candidate_compressed_rows",
          nullable_u64(candidate_compression_reached,
                       event.global.schur_compressed_rows));
  row.Add("baseline_preview_status",
          nullable_string(core.baseline_preview_available,
                          ov_msckf::msckf_update_preview_status_name(
                              event.global.nullspace_preview.status)));
  row.Add("baseline_preview_stage",
          nullable_string(core.baseline_preview_available,
                          ov_msckf::msckf_update_preview_stage_name(
                              event.global.nullspace_preview.stage)));
  row.Add("candidate_outcome", json_quote(proposal_outcome(event)));
  row.Add("candidate_preview_status",
          nullable_string(candidate_preview_reached,
                          ov_msckf::msckf_update_preview_status_name(
                              event.global.schur_preview.status)));
  row.Add("candidate_preview_stage",
          nullable_string(candidate_preview_reached,
                          ov_msckf::msckf_update_preview_stage_name(
                              event.global.schur_preview.stage)));
  row.Add("candidate_proposal_available",
          json_bool(context.candidate_proposal != nullptr));
  row.Add("baseline_preview_counters",
          counter_json(event.global.nullspace_preview.counters));
  row.Add("candidate_preview_counters",
          counter_json(event.global.schur_preview.counters));
  row.Add("baseline_commit_count", json_u64(core.baseline_commit_count));
  row.Add("baseline_transaction_mean_commits",
          json_u64(core.baseline_mean_commit_count));
  row.Add("baseline_covariance_commits",
          json_u64(core.baseline_covariance_commit_count));
  row.Add("baseline_expected_type_update_calls",
          json_u64(committed
                       ? event.commit_oracle.baseline_expected_type_update_calls
                       : 0U));
  row.Add("baseline_verified_nominal_fields",
          json_u64(committed
                       ? event.commit_oracle.baseline_verified_nominal_fields
                       : 0U));
  row.Add("baseline_nominal_mismatches",
          json_u64(committed ? event.commit_oracle.baseline_nominal_mismatches
                             : 0U));
  row.Add("baseline_covariance_mismatches",
          json_u64(committed
                       ? event.commit_oracle.baseline_covariance_mismatches
                       : 0U));
  row.Add("baseline_fej_mismatches",
          json_u64(committed ? event.commit_oracle.baseline_fej_mismatches
                             : 0U));
  row.Add("candidate_ekf_update_calls",
          json_u64(core.candidate_ekf_update_call_count));
  row.Add("candidate_type_update_calls",
          json_u64(core.candidate_type_update_call_count));
  row.Add("candidate_mean_writes",
          json_u64(core.candidate_mean_write_count));
  row.Add("candidate_covariance_writes",
          json_u64(core.candidate_covariance_write_count));
  row.Add("candidate_feature_writes",
          json_u64(core.candidate_feature_write_count));
  row.Add("state_block_rows", json_u64(context.state_block_rows));
  row.Add("covariance_block_rows", json_u64(context.covariance_block_rows));
  row.Add("all_block_rows_present", json_bool(context.all_block_rows_present));
  row.Add("math_passed", json_bool(context.math_passed));
  row.Add("config_sha256", json_quote(context.spec->config_sha256));
  row.Add("bag_sha256", json_quote(context.run->bag_sha256));
  row.Add("pair_index_sha256", json_quote(context.spec->serial_pairs_sha256));
  row.Add("resolved_parameters_sha256",
          json_quote(context.run->resolved_parameters_sha256));
  return row.Finish();
}

struct SequenceSummary {
  std::uint64_t attempted = 0U;
  std::uint64_t committing = 0U;
  std::uint64_t empty_input = 0U;
  std::uint64_t all_rejected = 0U;
  std::uint64_t empty_after_compression = 0U;
  std::uint64_t preflight_rejected = 0U;
  std::uint64_t internal_failure = 0U;
  bool bounds_available = false;
  std::uint64_t first_pair = 0U;
  std::uint64_t last_pair = 0U;
  std::uint64_t first_timestamp = 0U;
  std::uint64_t last_timestamp = 0U;
};

struct Summary {
  std::array<SequenceSummary, 3> sequences;
  std::uint64_t attempted_updates = 0U;
  std::uint64_t empty_input = 0U;
  std::uint64_t all_rejected = 0U;
  std::uint64_t empty_after_compression = 0U;
  std::uint64_t preflight_rejected = 0U;
  std::uint64_t internal_failure = 0U;
  std::uint64_t committing_updates = 0U;
  std::uint64_t raw_systems = 0U;
  std::uint64_t nullspace_gate_attempts = 0U;
  std::uint64_t schur_gate_attempts = 0U;
  std::uint64_t gate_union = 0U;
  std::uint64_t gate_intersection = 0U;
  std::uint64_t gate_matches = 0U;
  std::uint64_t row_denominator = 0U;
  std::uint64_t row_matches = 0U;
  std::map<std::string, std::uint64_t> disagreements;
  bool feature_statistics_passed = true;
  std::uint64_t state_expected = 0U;
  std::uint64_t state_seen = 0U;
  std::uint64_t covariance_expected = 0U;
  std::uint64_t covariance_seen = 0U;
  bool maximum_state_available = false;
  double maximum_state_ratio = 0.0;
  bool maximum_covariance_available = false;
  double maximum_covariance_ratio = 0.0;
  std::uint64_t candidate_missing = 0U;
  std::uint64_t baseline_commit_mismatches = 0U;
  std::uint64_t shadow_ekf_calls = 0U;
  std::uint64_t shadow_type_calls = 0U;
  std::uint64_t shadow_mean_writes = 0U;
  std::uint64_t shadow_covariance_writes = 0U;
  std::uint64_t shadow_feature_writes = 0U;
  AggregateCounters repair;
  bool gate_passed = false;
  bool math_passed = true;

  Summary() {
    for (const char *name : {
             "both_match_accept", "both_match_reject",
             "boolean_nullspace_accept_schur_reject",
             "boolean_nullspace_reject_schur_accept", "nullspace_only",
             "schur_only", "neither_decision"}) {
      disagreements.emplace(name, 0U);
    }
  }
};

void count_terminal(Summary &summary,
                    const ov_msckf::CP2TraceJournalDecodedEvent &event) {
  SequenceSummary &sequence = summary.sequences.at(
      static_cast<std::size_t>(event.sequence_index));
  summary.attempted_updates =
      add_or_fail(summary.attempted_updates, 1U, "attempted updates");
  sequence.attempted = add_or_fail(sequence.attempted, 1U,
                                  "sequence attempted updates");
  if (!sequence.bounds_available) {
    sequence.bounds_available = true;
    sequence.first_pair = event.pair_index;
    sequence.first_timestamp = event.camera_timestamp_ns;
  }
  sequence.last_pair = event.pair_index;
  sequence.last_timestamp = event.camera_timestamp_ns;
  auto increment = [](std::uint64_t &global, std::uint64_t &local,
                      const char *label) {
    global = add_or_fail(global, 1U, label);
    local = add_or_fail(local, 1U, label);
  };
  switch (event.core.terminal_status) {
  case ov_msckf::CP2UpdateTerminalStatus::kEmptyInput:
    increment(summary.empty_input, sequence.empty_input, "empty-input count");
    break;
  case ov_msckf::CP2UpdateTerminalStatus::kAllRejected:
    increment(summary.all_rejected, sequence.all_rejected,
              "all-rejected count");
    break;
  case ov_msckf::CP2UpdateTerminalStatus::kEmptyAfterCompression:
    increment(summary.empty_after_compression,
              sequence.empty_after_compression,
              "empty-after-compression count");
    break;
  case ov_msckf::CP2UpdateTerminalStatus::kPreflightRejected:
    increment(summary.preflight_rejected, sequence.preflight_rejected,
              "preflight-rejected count");
    break;
  case ov_msckf::CP2UpdateTerminalStatus::kCommittedCounted:
    increment(summary.committing_updates, sequence.committing,
              "committing-update count");
    break;
  case ov_msckf::CP2UpdateTerminalStatus::kInternalFailure:
    increment(summary.internal_failure, sequence.internal_failure,
              "internal-failure count");
    break;
  }
}

std::string sequence_summary_json(std::size_t index,
                                  const SequenceSummary &summary) {
  JsonObjectWriter row;
  row.Add("sequence_index", json_u64(static_cast<std::uint64_t>(index)));
  row.Add("sequence_id", json_quote(kSequenceIds[index]));
  row.Add("attempted_updates", json_u64(summary.attempted));
  row.Add("committing_updates", json_u64(summary.committing));
  row.Add("empty_input", json_u64(summary.empty_input));
  row.Add("all_rejected", json_u64(summary.all_rejected));
  row.Add("empty_after_compression",
          json_u64(summary.empty_after_compression));
  row.Add("preflight_rejected", json_u64(summary.preflight_rejected));
  row.Add("internal_failure", json_u64(summary.internal_failure));
  row.Add("first_pair_index",
          nullable_u64(summary.bounds_available, summary.first_pair));
  row.Add("last_pair_index",
          nullable_u64(summary.bounds_available, summary.last_pair));
  row.Add("first_camera_timestamp_ns",
          nullable_u64(summary.bounds_available, summary.first_timestamp));
  row.Add("last_camera_timestamp_ns",
          nullable_u64(summary.bounds_available, summary.last_timestamp));
  return row.Finish();
}

double count_ratio(std::uint64_t numerator, std::uint64_t denominator,
                   const char *label) {
  constexpr std::uint64_t kMaximumExactBinary64Integer =
      (UINT64_C(1) << 53U) - UINT64_C(1);
  if (denominator == 0U || numerator > kMaximumExactBinary64Integer ||
      denominator > kMaximumExactBinary64Integer) {
    throw AssembleError(std::string(label) +
                        " population is not an exactly convertible ratio");
  }
  require_tonearest(label);
  return static_cast<double>(numerator) / static_cast<double>(denominator);
}

std::string summary_json(const Summary &summary) {
  std::array<std::string, 3> sequences;
  for (std::size_t index = 0U; index < sequences.size(); ++index)
    sequences[index] = sequence_summary_json(index, summary.sequences[index]);
  JsonObjectWriter disagreement;
  for (const auto &entry : summary.disagreements)
    disagreement.Add(entry.first, json_u64(entry.second));
  JsonObjectWriter writes;
  writes.Add("ekf_update_calls", json_u64(summary.shadow_ekf_calls));
  writes.Add("type_update_calls", json_u64(summary.shadow_type_calls));
  writes.Add("mean_writes", json_u64(summary.shadow_mean_writes));
  writes.Add("covariance_writes", json_u64(summary.shadow_covariance_writes));
  writes.Add("feature_writes", json_u64(summary.shadow_feature_writes));
  const bool passed_pre_replay =
      summary.committing_updates >= kMinimumCommittingUpdates &&
      summary.internal_failure == 0U && summary.gate_passed &&
      summary.math_passed && summary.candidate_missing == 0U &&
      summary.baseline_commit_mismatches == 0U &&
      summary.shadow_ekf_calls == 0U && summary.shadow_type_calls == 0U &&
      summary.shadow_mean_writes == 0U &&
      summary.shadow_covariance_writes == 0U &&
      summary.shadow_feature_writes == 0U && counters_zero(summary.repair) &&
      summary.maximum_state_available && summary.maximum_covariance_available;
  JsonObjectWriter row;
  row.Add("sequence_summaries",
          json_array(sequences.begin(), sequences.end(),
                     [](const std::string &value) { return value; }));
  row.Add("attempted_updates", json_u64(summary.attempted_updates));
  row.Add("empty_input", json_u64(summary.empty_input));
  row.Add("all_rejected", json_u64(summary.all_rejected));
  row.Add("empty_after_compression",
          json_u64(summary.empty_after_compression));
  row.Add("preflight_rejected", json_u64(summary.preflight_rejected));
  row.Add("internal_failure", json_u64(summary.internal_failure));
  row.Add("committing_updates", json_u64(summary.committing_updates));
  row.Add("minimum_committing_updates", json_u64(kMinimumCommittingUpdates));
  row.Add("raw_systems", json_u64(summary.raw_systems));
  row.Add("nullspace_gate_attempts",
          json_u64(summary.nullspace_gate_attempts));
  row.Add("schur_gate_attempts", json_u64(summary.schur_gate_attempts));
  row.Add("gate_union_denominator", json_u64(summary.gate_union));
  row.Add("gate_intersection", json_u64(summary.gate_intersection));
  row.Add("gate_match_numerator", json_u64(summary.gate_matches));
  row.Add("gate_ratio",
          nullable_f64(summary.gate_union != 0U,
                       summary.gate_union == 0U
                           ? 0.0
                           : count_ratio(summary.gate_matches,
                                         summary.gate_union, "gate ratio")));
  row.Add("row_denominator", json_u64(summary.row_denominator));
  row.Add("row_match_numerator", json_u64(summary.row_matches));
  row.Add("row_ratio",
          nullable_f64(summary.row_denominator != 0U,
                       summary.row_denominator == 0U
                           ? 0.0
                           : count_ratio(summary.row_matches,
                                         summary.row_denominator,
                                         "raw-row ratio")));
  row.Add("disagreement_counts", disagreement.Finish());
  row.Add("per_feature_statistics_passed",
          json_bool(summary.feature_statistics_passed));
  row.Add("state_blocks_expected", json_u64(summary.state_expected));
  row.Add("state_blocks_seen", json_u64(summary.state_seen));
  row.Add("covariance_blocks_expected",
          json_u64(summary.covariance_expected));
  row.Add("covariance_blocks_seen", json_u64(summary.covariance_seen));
  row.Add("maximum_state_ratio",
          nullable_f64(summary.maximum_state_available,
                       summary.maximum_state_ratio));
  row.Add("maximum_covariance_ratio",
          nullable_f64(summary.maximum_covariance_available,
                       summary.maximum_covariance_ratio));
  row.Add("candidate_missing_proposals", json_u64(summary.candidate_missing));
  row.Add("baseline_commit_mismatches",
          json_u64(summary.baseline_commit_mismatches));
  row.Add("shadow_write_totals", writes.Finish());
  row.Add("repair_fallback_totals", counter_json(summary.repair));
  row.Add("gate_passed", json_bool(summary.gate_passed));
  row.Add("math_passed", json_bool(summary.math_passed));
  row.Add("passed_pre_replay", json_bool(passed_pre_replay));
  return row.Finish() + "\n";
}

bool same_binary64(double left, double right) noexcept {
  std::uint64_t left_bits = 0U;
  std::uint64_t right_bits = 0U;
  std::memcpy(&left_bits, &left, sizeof(left_bits));
  std::memcpy(&right_bits, &right, sizeof(right_bits));
  return left_bits == right_bits;
}

std::string expected_agreement(bool null_available, bool null_accept,
                               bool schur_available, bool schur_accept) {
  if (!null_available && !schur_available) return "neither_decision";
  if (!null_available) return "schur_only";
  if (!schur_available) return "nullspace_only";
  if (null_accept && schur_accept) return "both_match_accept";
  if (!null_accept && !schur_accept) return "both_match_reject";
  return null_accept ? "boolean_nullspace_accept_schur_reject"
                     : "boolean_nullspace_reject_schur_accept";
}

void append_line(std::string &file, const std::string &row) {
  const std::size_t maximum = std::numeric_limits<std::size_t>::max();
  if (row.size() == maximum || file.size() > maximum - row.size() - 1U)
    throw AssembleError("JSONL output size overflows size_t");
  file += row;
  file.push_back('\n');
}

int assemble_main(int argc, char **argv) {
  if (argc != 5 || argv == nullptr || argv[0] == nullptr ||
      argv[1] == nullptr || argv[2] == nullptr || argv[3] == nullptr ||
      argv[4] == nullptr || std::string(argv[1]) != "--spec" ||
      std::string(argv[3]) != "--output-dir") {
    throw AssembleError(
        "usage: cp2_recorded_assemble --spec ABS_JSON --output-dir ABS_EMPTY_DIR");
  }
  const std::string spec_path(argv[2]);
  const std::string output_path(argv[4]);
  if (!normalized_absolute_path(spec_path) ||
      !normalized_absolute_path(output_path) || spec_path == output_path) {
    throw AssembleError("assembler paths must be distinct normalized absolute paths");
  }
  OwnedDescriptor spec_descriptor = open_absolute(spec_path, O_RDONLY, false);
  const FileBytes spec_bytes = read_file(spec_descriptor.get(), kMaximumJsonBytes);
  const AssemblySpec spec = parse_spec(spec_bytes);
  OwnedDescriptor output = open_absolute(output_path, O_RDONLY, true);
  const FileIdentity output_identity = file_identity(output.get(), true);
  require_empty_directory(output.get());

  OwnedDescriptor serial_descriptor =
      open_absolute(spec.serial_pairs, O_RDONLY, false);
  const FileBytes serial = read_file(serial_descriptor.get(), kMaximumJsonBytes);
  if (serial.sha256 != spec.serial_pairs_sha256) {
    throw AssembleError("serial-pair SHA-256 differs from assembly spec");
  }
  std::set<std::pair<dev_t, ino_t>> input_identities;
  const auto retain_identity = [&input_identities, &output_identity](
                                   const FileIdentity &identity) {
    if (same_file(identity, output_identity) ||
        !input_identities.emplace(identity.device, identity.inode).second) {
      throw AssembleError("assembly input aliases another input/output");
    }
  };
  retain_identity(spec_bytes.identity);
  retain_identity(serial.identity);
  const std::map<SerialOwnerKey, SerialOwner> serial_owners =
      parse_serial_pairs(serial);

  std::array<ov_msckf::CP2TraceJournalDecoded, 3> journals;
  std::vector<ov_msckf::CP2StateTraceFrame> state_frames;
  std::vector<ov_msckf::CP2ProposalTraceFrame> proposal_frames;
  std::vector<ov_msckf::CP2RawSystemTraceFrame> raw_frames;
  for (std::size_t index = 0U; index < spec.runs.size(); ++index) {
    OwnedDescriptor descriptor =
        open_absolute(spec.runs[index].journal, O_RDONLY, false);
    FileBytes journal_bytes = read_file(descriptor.get(), kMaximumJournalBytes);
    retain_identity(journal_bytes.identity);
    ov_msckf::CP2TraceJournalDecodeResult decoded =
        ov_msckf::CP2TraceJournalCodec::Decode(journal_bytes.bytes);
    if (!decoded.accepted()) {
      throw AssembleError(std::string("journal decode failed: ") +
                          ov_msckf::cp2_trace_journal_decode_status_name(
                              decoded.status));
    }
    journals[index] = std::move(decoded.journal);
    for (const auto &event : journals[index].events) {
      if (event.sequence_index != index) {
        throw AssembleError("journal sequence differs from assembly spec");
      }
    }
    std::vector<ov_msckf::CP2StateTraceFrame> local_states =
        ov_msckf::CP2StateTraceCodec::DecodeStateFile(
            journals[index].state_file_bytes);
    std::vector<ov_msckf::CP2ProposalTraceFrame> local_proposals =
        ov_msckf::CP2TraceCodec::DecodeProposalFile(
            journals[index].proposal_file_bytes);
    std::vector<ov_msckf::CP2RawSystemTraceFrame> local_raw =
        ov_msckf::CP2TraceCodec::DecodeRawSystemFile(
            journals[index].raw_system_file_bytes);
    state_frames.insert(state_frames.end(),
                        std::make_move_iterator(local_states.begin()),
                        std::make_move_iterator(local_states.end()));
    proposal_frames.insert(proposal_frames.end(),
                           std::make_move_iterator(local_proposals.begin()),
                           std::make_move_iterator(local_proposals.end()));
    raw_frames.insert(raw_frames.end(),
                      std::make_move_iterator(local_raw.begin()),
                      std::make_move_iterator(local_raw.end()));
  }

  ov_msckf::CP2EncodedStateFile encoded_state =
      ov_msckf::CP2StateTraceCodec::EncodeStateFile(state_frames);
  ov_msckf::CP2EncodedProposalFile encoded_proposal =
      ov_msckf::CP2TraceCodec::EncodeProposalFile(proposal_frames);
  ov_msckf::CP2EncodedRawSystemFile encoded_raw =
      ov_msckf::CP2TraceCodec::EncodeRawSystemFile(raw_frames);
  if (encoded_state.payloads.size() != state_frames.size() ||
      encoded_proposal.payloads.size() != proposal_frames.size() ||
      encoded_raw.payloads.size() != raw_frames.size()) {
    throw AssembleError("global codec payload-reference population differs");
  }
  for (std::size_t index = 0U; index < state_frames.size(); ++index)
    state_frames[index].payload = encoded_state.payloads[index];
  for (std::size_t index = 0U; index < proposal_frames.size(); ++index)
    proposal_frames[index].payload = encoded_proposal.payloads[index];
  for (std::size_t index = 0U; index < raw_frames.size(); ++index)
    raw_frames[index].payload = encoded_raw.payloads[index];

  std::map<StateKey, const ov_msckf::CP2StateTraceFrame *> state_by_key;
  for (const auto &frame : state_frames) {
    const StateKey key{frame.invocation.sequence_index,
                       frame.invocation.pair_index,
                       frame.invocation.invocation_id,
                       static_cast<std::uint64_t>(frame.phase)};
    if (!state_by_key.emplace(key, &frame).second)
      throw AssembleError("duplicate global state frame");
  }
  std::map<ProposalKey, const ov_msckf::CP2ProposalTraceFrame *> proposal_by_key;
  for (const auto &frame : proposal_frames) {
    const ProposalKey key{frame.invocation.sequence_index,
                          frame.invocation.pair_index,
                          frame.invocation.invocation_id,
                          static_cast<std::uint64_t>(frame.role)};
    if (!proposal_by_key.emplace(key, &frame).second)
      throw AssembleError("duplicate global proposal frame");
  }
  std::map<InvocationKey,
           std::vector<const ov_msckf::CP2RawSystemTraceFrame *>> raw_by_key;
  for (const auto &frame : raw_frames)
    raw_by_key[invocation_key(frame.invocation)].push_back(&frame);

  std::vector<EventContext> contexts;
  std::set<SerialOwnerKey> joined_owners;
  std::set<StateKey> consumed_states;
  std::set<ProposalKey> consumed_proposals;
  std::set<InvocationKey> consumed_raw_groups;
  for (std::size_t sequence = 0U; sequence < journals.size(); ++sequence) {
    for (const auto &event : journals[sequence].events) {
      const SerialOwnerKey owner_key{event.sequence_index, event.invocation_id};
      const auto owner = serial_owners.find(owner_key);
      if (owner == serial_owners.end() ||
          owner->second.pair_index != event.pair_index ||
          owner->second.camera_timestamp_ns != event.camera_timestamp_ns ||
          !joined_owners.insert(owner_key).second) {
        throw AssembleError("journal event does not join one serial owner");
      }
      EventContext context;
      context.spec = &spec;
      context.run = &spec.runs[sequence];
      context.event = &event;
      const bool has_raw = event.raw_system_count != 0U;
      const bool is_committed = event.core.terminal_status ==
          ov_msckf::CP2UpdateTerminalStatus::kCommittedCounted;
      for (std::size_t phase = 0U; phase < 4U; ++phase) {
        const StateKey state_key{event.sequence_index, event.pair_index,
                                 event.invocation_id,
                                 static_cast<std::uint64_t>(phase)};
        const auto found = state_by_key.find(state_key);
        const bool expected = has_raw && (phase < 2U || is_committed);
        if ((found != state_by_key.end()) != expected)
          throw AssembleError("state frame population differs from event terminal");
        if (found != state_by_key.end()) {
          context.states[phase] = found->second;
          consumed_states.insert(state_key);
        }
      }
      const ProposalKey baseline_key{event.sequence_index, event.pair_index,
                                     event.invocation_id, 0U};
      const ProposalKey candidate_key{event.sequence_index, event.pair_index,
                                      event.invocation_id, 1U};
      const auto baseline = proposal_by_key.find(baseline_key);
      const auto candidate = proposal_by_key.find(candidate_key);
      if ((baseline != proposal_by_key.end()) !=
              event.core.baseline_proposal_payload_available ||
          (candidate != proposal_by_key.end()) !=
              event.core.candidate_proposal_payload_available)
        throw AssembleError("proposal frame population differs from event flags");
      if (baseline != proposal_by_key.end()) {
        context.baseline_proposal = baseline->second;
        consumed_proposals.insert(baseline_key);
      }
      if (candidate != proposal_by_key.end()) {
        context.candidate_proposal = candidate->second;
        consumed_proposals.insert(candidate_key);
      }
      const InvocationKey raw_key{event.sequence_index, event.pair_index,
                                  event.invocation_id};
      const auto raw_group = raw_by_key.find(raw_key);
      if (raw_group != raw_by_key.end()) {
        context.raw_frames = raw_group->second;
        consumed_raw_groups.insert(raw_key);
      }
      if (context.raw_frames.size() != event.feature_summaries.size() ||
          context.raw_frames.size() != event.raw_system_count) {
        throw AssembleError("event raw/feature population differs");
      }
      context.baseline_digests = ov_msckf::CP2TraceCodec::AcceptedFeatureDigests(
          event.baseline_accepted_ids);
      context.candidate_digests = ov_msckf::CP2TraceCodec::AcceptedFeatureDigests(
          event.candidate_accepted_ids);
      contexts.push_back(std::move(context));
    }
  }
  if (joined_owners.size() != serial_owners.size()) {
    throw AssembleError("serial/update invocation ownership is not one-to-one");
  }
  if (consumed_states.size() != state_by_key.size() ||
      consumed_proposals.size() != proposal_by_key.size() ||
      consumed_raw_groups.size() != raw_by_key.size()) {
    throw AssembleError("global codec files contain an orphan frame");
  }

  Summary summary;
  std::string updates_jsonl;
  std::string features_jsonl;
  std::string state_jsonl;
  std::string covariance_jsonl;
  for (EventContext &context : contexts) {
    const auto &event = *context.event;
    const auto &core = event.core;
    count_terminal(summary, event);
    summary.raw_systems = add_or_fail(summary.raw_systems,
                                      event.raw_system_count, "raw systems");
    summary.shadow_ekf_calls = add_or_fail(
        summary.shadow_ekf_calls, core.candidate_ekf_update_call_count,
        "shadow EKF calls");
    summary.shadow_type_calls = add_or_fail(
        summary.shadow_type_calls, core.candidate_type_update_call_count,
        "shadow Type calls");
    summary.shadow_mean_writes = add_or_fail(
        summary.shadow_mean_writes, core.candidate_mean_write_count,
        "shadow mean writes");
    summary.shadow_covariance_writes = add_or_fail(
        summary.shadow_covariance_writes,
        core.candidate_covariance_write_count, "shadow covariance writes");
    summary.shadow_feature_writes = add_or_fail(
        summary.shadow_feature_writes, core.candidate_feature_write_count,
        "shadow feature writes");
    add_counters(summary.repair, event.global.nullspace_preview.counters);
    add_counters(summary.repair, event.global.schur_preview.counters);

    std::vector<std::uint64_t> baseline_decisions;
    std::vector<std::uint64_t> candidate_decisions;
    for (std::size_t ordinal = 0U; ordinal < event.feature_summaries.size();
         ++ordinal) {
      const auto &feature = event.feature_summaries[ordinal];
      const auto &raw = *context.raw_frames[ordinal];
      if (raw.feature_ordinal != ordinal) {
        throw AssembleError("raw feature ordinal is noncontiguous");
      }
      append_line(features_jsonl,
                  feature_row_json(context, ordinal, raw, feature));
      add_counters(summary.repair, feature.nullspace_reducer_counters);
      add_counters(summary.repair, feature.schur_reducer_counters);
      const bool null_available =
          feature.nullspace_gate.evidence_decision_available;
      const bool schur_available =
          feature.schur_gate.evidence_decision_available;
      if (null_available) {
        summary.nullspace_gate_attempts = add_or_fail(
            summary.nullspace_gate_attempts, 1U, "nullspace gate attempts");
        if (feature.nullspace_gate.evidence_accept)
          baseline_decisions.push_back(feature.feature_id);
      }
      if (schur_available) {
        summary.schur_gate_attempts = add_or_fail(
            summary.schur_gate_attempts, 1U, "Schur gate attempts");
        if (feature.schur_gate.evidence_accept)
          candidate_decisions.push_back(feature.feature_id);
      }
      const std::string agreement = expected_agreement(
          null_available, feature.nullspace_gate.evidence_accept,
          schur_available, feature.schur_gate.evidence_accept);
      const std::string recorded =
          ov_msckf::cp2_gate_agreement_class_name(feature.agreement_class);
      if (agreement != recorded) context.feature_math_passed = false;
      summary.disagreements.at(agreement) = add_or_fail(
          summary.disagreements.at(agreement), 1U, "agreement-class count");
      if (agreement != "neither_decision") {
        summary.gate_union = add_or_fail(summary.gate_union, 1U, "gate union");
        summary.row_denominator = add_or_fail(
            summary.row_denominator, feature.raw_rows, "row denominator");
      }
      if (null_available && schur_available)
        summary.gate_intersection = add_or_fail(
            summary.gate_intersection, 1U, "gate intersection");
      const bool match = agreement == "both_match_accept" ||
                         agreement == "both_match_reject";
      const std::uint64_t expected_weight = match ? feature.raw_rows : 0U;
      if (feature.raw_row_match_weight != expected_weight)
        context.feature_math_passed = false;
      if (match) {
        summary.gate_matches = add_or_fail(summary.gate_matches, 1U,
                                           "gate matches");
        summary.row_matches = add_or_fail(summary.row_matches,
                                          feature.raw_rows, "row matches");
      }
      const bool expected_statistics_required =
          feature.nullspace_status ==
              ov_msckf::CP2NullspaceReductionStatus::kAccepted &&
          feature.schur_status == ov_msckf::SchurReductionStatus::kAccepted;
      const bool stats =
          feature.statistics_comparison_required ==
              expected_statistics_required &&
          statistic_valid(feature.statistics_comparison_required,
                          feature.lambda_comparison) &&
          statistic_valid(feature.statistics_comparison_required,
                          feature.eta_comparison) &&
          statistic_valid(feature.statistics_comparison_required,
                          feature.gamma_comparison);
      context.feature_math_passed = context.feature_math_passed && stats &&
          feature.raw_layout_valid &&
          counters_zero(feature.nullspace_reducer_counters) &&
          counters_zero(feature.schur_reducer_counters);
      summary.feature_statistics_passed =
          summary.feature_statistics_passed && stats;
    }
    if (baseline_decisions != event.baseline_accepted_ids ||
        candidate_decisions != event.candidate_accepted_ids) {
      context.feature_math_passed = false;
    }

    const bool raw = event.raw_system_count != 0U;
    const bool committed = core.terminal_status ==
                           ov_msckf::CP2UpdateTerminalStatus::kCommittedCounted;
    const bool phase01 = !raw ||
        (context.states[0] != nullptr && context.states[1] != nullptr &&
         ov_msckf::CP2CompositeStateAdapter::CanonicallyEqual(
             context.states[0]->snapshot, context.states[1]->snapshot));
    if ((raw && (context.states[0] == nullptr || context.states[1] == nullptr)) ||
        (!raw && (context.states[0] != nullptr || context.states[1] != nullptr)) ||
        core.phase01_canonical_equal != (raw && phase01)) {
      context.commit_exact = false;
    } else if (!committed) {
      context.commit_exact = context.states[2] == nullptr &&
                             context.states[3] == nullptr &&
                             context.baseline_proposal == nullptr &&
                             !core.baseline_commit_oracle_available &&
                             core.baseline_commit_count == 0U &&
                             core.baseline_mean_commit_count == 0U &&
                             core.baseline_covariance_commit_count == 0U;
    } else if (context.states[0] != nullptr && context.states[2] != nullptr &&
               context.states[3] != nullptr &&
               context.baseline_proposal != nullptr) {
      ov_msckf::MSCKFUpdatePreviewResult baseline_preview;
      baseline_preview.diagnostics.status =
          ov_msckf::MSCKFUpdatePreviewStatus::kAccepted;
      baseline_preview.diagnostics.stage =
          ov_msckf::MSCKFUpdatePreviewStage::kAccepted;
      baseline_preview.dx = context.baseline_proposal->proposal.dx;
      baseline_preview.P_plus = context.baseline_proposal->proposal.P_plus;
      ov_msckf::CP2CompositeStateSnapshot derived;
      std::uint64_t type_calls = 0U;
      const ov_msckf::CP2CompositeStateStatus status =
          ov_msckf::CP2CompositeStateAdapter::BuildExpected(
              context.states[0]->snapshot, baseline_preview, derived,
              type_calls);
      if (status == ov_msckf::CP2CompositeStateStatus::kAccepted) {
        const ov_msckf::CP2CommitOracleResult recomputed =
            ov_msckf::CP2CommitOracle::Compare(
                context.states[0]->snapshot, derived,
                context.states[3]->snapshot, type_calls);
        context.commit_exact =
            ov_msckf::CP2CompositeStateAdapter::CanonicallyEqual(
                derived, context.states[2]->snapshot) &&
            ov_msckf::CP2CompositeStateAdapter::CanonicallyEqual(
                derived, context.states[3]->snapshot) && recomputed.passed &&
            oracle_equal(recomputed, event.commit_oracle) &&
            core.baseline_commit_oracle_available &&
            core.baseline_commit_count == 1U &&
            core.baseline_mean_commit_count == 1U &&
            core.baseline_covariance_commit_count == 1U;
      }
    }

    if (committed) {
      if (context.states[0] == nullptr || context.baseline_proposal == nullptr)
        throw AssembleError("committed update lacks baseline block inputs");
      const auto &prior = context.states[0]->snapshot;
      const std::uint64_t blocks =
          static_cast<std::uint64_t>(prior.semantic_blocks.size());
      const std::uint64_t covariance_blocks =
          multiply_or_fail(blocks, blocks, "covariance block population");
      context.state_block_rows = blocks;
      context.covariance_block_rows = covariance_blocks;
      summary.state_expected = add_or_fail(summary.state_expected, blocks,
                                           "state blocks expected");
      summary.covariance_expected = add_or_fail(
          summary.covariance_expected, covariance_blocks,
          "covariance blocks expected");
      const auto &baseline = context.baseline_proposal->proposal;
      const bool candidate_available = context.candidate_proposal != nullptr;
      const ov_msckf::CP2ProposalTracePayload *candidate_payload =
          candidate_available ? &context.candidate_proposal->proposal : nullptr;
      if (baseline.dx.rows() != prior.covariance.rows() ||
          baseline.P_plus.rows() != prior.covariance.rows() ||
          baseline.P_plus.cols() != prior.covariance.cols() ||
          (candidate_available &&
           (candidate_payload->dx.rows() != baseline.dx.rows() ||
            candidate_payload->P_plus.rows() != baseline.P_plus.rows() ||
            candidate_payload->P_plus.cols() != baseline.P_plus.cols()))) {
        throw AssembleError("proposal dimensions differ from prior state");
      }
      const Eigen::VectorXd empty_vector;
      const Eigen::MatrixXd empty_matrix;
      for (std::size_t index = 0U; index < prior.semantic_blocks.size(); ++index) {
        const auto &block = prior.semantic_blocks[index];
        if (block.covariance_id >
                static_cast<std::uint64_t>(std::numeric_limits<Eigen::Index>::max()) ||
            block.error_size >
                static_cast<std::uint64_t>(std::numeric_limits<Eigen::Index>::max()))
          throw AssembleError("semantic block exceeds Eigen index");
        const Eigen::Index start = static_cast<Eigen::Index>(block.covariance_id);
        const Eigen::Index size = static_cast<Eigen::Index>(block.error_size);
        const Eigen::VectorXd candidate = candidate_available
            ? candidate_payload->dx.segment(start, size).eval()
            : empty_vector;
        const BlockComparison comparison = compare_block(
            baseline.dx.segment(start, size), candidate_available, candidate);
        append_line(state_jsonl,
                    state_block_row_json(context, index, block, comparison));
        summary.state_seen = add_or_fail(summary.state_seen, 1U,
                                         "state blocks seen");
        context.blocks_passed = context.blocks_passed && comparison.passed;
        if (comparison.comparison_available) {
          summary.maximum_state_ratio = summary.maximum_state_available
              ? std::max(summary.maximum_state_ratio, comparison.ratio)
              : comparison.ratio;
          summary.maximum_state_available = true;
        }
      }
      for (std::size_t row_index = 0U;
           row_index < prior.semantic_blocks.size(); ++row_index) {
        const auto &row_block = prior.semantic_blocks[row_index];
        const Eigen::Index row_start =
            static_cast<Eigen::Index>(row_block.covariance_id);
        const Eigen::Index row_size =
            static_cast<Eigen::Index>(row_block.error_size);
        for (std::size_t column_index = 0U;
             column_index < prior.semantic_blocks.size(); ++column_index) {
          const auto &column_block = prior.semantic_blocks[column_index];
          const Eigen::Index column_start =
              static_cast<Eigen::Index>(column_block.covariance_id);
          const Eigen::Index column_size =
              static_cast<Eigen::Index>(column_block.error_size);
          const Eigen::MatrixXd candidate = candidate_available
              ? candidate_payload->P_plus.block(row_start, column_start,
                                                 row_size, column_size).eval()
              : empty_matrix;
          const BlockComparison comparison = compare_block(
              baseline.P_plus.block(row_start, column_start, row_size,
                                    column_size),
              candidate_available, candidate);
          append_line(covariance_jsonl,
                      covariance_block_row_json(
                          context, row_index, column_index, row_block,
                          column_block, comparison));
          summary.covariance_seen = add_or_fail(
              summary.covariance_seen, 1U, "covariance blocks seen");
          context.blocks_passed = context.blocks_passed && comparison.passed;
          if (comparison.comparison_available) {
            summary.maximum_covariance_ratio =
                summary.maximum_covariance_available
                    ? std::max(summary.maximum_covariance_ratio,
                               comparison.ratio)
                    : comparison.ratio;
            summary.maximum_covariance_available = true;
          }
        }
      }
      if (!candidate_available)
        summary.candidate_missing = add_or_fail(
            summary.candidate_missing, 1U, "candidate-missing proposals");
    }

    const bool gamma_valid =
        event.global.nullspace_gamma_status !=
            ov_msckf::CP2GammaStatus::kNonfinite &&
        event.global.schur_gamma_status != ov_msckf::CP2GammaStatus::kNonfinite &&
        event.global.nullspace_gamma_status == core.baseline_gamma_status &&
        (core.baseline_gamma_status != ov_msckf::CP2GammaStatus::kAvailable ||
         same_binary64(core.baseline_gamma,
                       event.global.nullspace_retained_gamma));
    const bool writes_zero = core.candidate_ekf_update_call_count == 0U &&
                             core.candidate_type_update_call_count == 0U &&
                             core.candidate_mean_write_count == 0U &&
                             core.candidate_covariance_write_count == 0U &&
                             core.candidate_feature_write_count == 0U;
    const bool previews_clean =
        counters_zero(event.global.nullspace_preview.counters) &&
        counters_zero(event.global.schur_preview.counters);
    context.math_passed = core.online_math_evidence_passed && phase01 &&
                          context.commit_exact &&
                          context.feature_math_passed && context.blocks_passed &&
                          gamma_valid && writes_zero && previews_clean &&
                          (!committed || context.candidate_proposal != nullptr) &&
                          core.terminal_status !=
                              ov_msckf::CP2UpdateTerminalStatus::kInternalFailure;
    const bool commit_mismatch = core.baseline_commit_mismatch ||
        (committed && !context.commit_exact) ||
        event.commit_oracle.baseline_nominal_mismatches != 0U ||
        event.commit_oracle.baseline_covariance_mismatches != 0U ||
        event.commit_oracle.baseline_fej_mismatches != 0U;
    if (commit_mismatch)
      summary.baseline_commit_mismatches = add_or_fail(
          summary.baseline_commit_mismatches, 1U,
          "baseline commit mismatches");
    summary.math_passed = summary.math_passed && context.math_passed;
    append_line(updates_jsonl, update_row_json(context));
  }

  const std::uint64_t gate_left =
      multiply_or_fail(1000U, summary.gate_matches, "gate cross-product");
  const std::uint64_t gate_right =
      multiply_or_fail(999U, summary.gate_union, "gate cross-product");
  const std::uint64_t row_left =
      multiply_or_fail(1000U, summary.row_matches, "row cross-product");
  const std::uint64_t row_right =
      multiply_or_fail(999U, summary.row_denominator, "row cross-product");
  summary.gate_passed = summary.gate_union != 0U &&
                        summary.row_denominator != 0U &&
                        gate_left >= gate_right && row_left >= row_right;
  summary.math_passed = summary.math_passed &&
                        summary.feature_statistics_passed &&
                        summary.state_expected == summary.state_seen &&
                        summary.covariance_expected == summary.covariance_seen &&
                        summary.candidate_missing == 0U &&
                        summary.baseline_commit_mismatches == 0U &&
                        counters_zero(summary.repair);

  write_new_file(output.get(), "updates.jsonl", updates_jsonl);
  write_new_file(output.get(), "features.jsonl", features_jsonl);
  write_new_file(output.get(), "state_blocks.jsonl", state_jsonl);
  write_new_file(output.get(), "covariance_blocks.jsonl", covariance_jsonl);
  write_new_file(output.get(), "state_snapshot_payloads.bin",
                 encoded_state.bytes);
  write_new_file(output.get(), "proposal_payloads.bin",
                 encoded_proposal.bytes);
  write_new_file(output.get(), "raw_system_payloads.bin", encoded_raw.bytes);
  write_new_file(output.get(), "summary.json", summary_json(summary));
  if (::fsync(output.get()) != 0)
    throw AssembleError("cannot sync output directory");
  return 0;
}

} // namespace

namespace ov_msckf {

int cp2_recorded_assemble_entry(int argc, char **argv) noexcept {
  try {
    return assemble_main(argc, argv);
  } catch (const std::exception &error) {
    const std::string message =
        std::string("cp2_recorded_assemble: ") + error.what() + "\n";
    const ssize_t ignored =
        ::write(STDERR_FILENO, message.data(), message.size());
    (void)ignored;
    return 1;
  } catch (...) {
    const char message[] = "cp2_recorded_assemble: unknown failure\n";
    const ssize_t ignored =
        ::write(STDERR_FILENO, message, sizeof(message) - 1U);
    (void)ignored;
    return 1;
  }
}

} // namespace ov_msckf

#ifndef CP2_RECORDED_ASSEMBLE_NO_MAIN
int main(int argc, char **argv) {
  return ov_msckf::cp2_recorded_assemble_entry(argc, argv);
}
#endif
