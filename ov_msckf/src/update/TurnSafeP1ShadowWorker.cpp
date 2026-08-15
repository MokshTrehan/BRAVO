/*
 * SPDX-License-Identifier: GPL-3.0-or-later
 * TurnSafe T1 P1 deterministic offline archive scanner.
 */

#include "TurnSafePilotShadow.h"

#include <Eigen/Core>

#include <jsoncpp/json/json.h>
#include <openssl/evp.h>
#include <zstd.h>

#include <algorithm>
#include <array>
#include <cctype>
#include <cerrno>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <cstdlib>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <limits>
#include <locale>
#include <map>
#include <memory>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <sys/stat.h>
#include <unistd.h>
#include <utility>
#include <vector>

namespace {

using ov_msckf::TurnSafeCertificateCamera;
using ov_msckf::TurnSafeCertificateObservation;
using ov_msckf::TurnSafePairGroupKey;
using ov_msckf::TurnSafePairGroupResult;
using ov_msckf::TurnSafePairMemberKey;
using ov_msckf::TurnSafePilotCallbackInput;
using ov_msckf::TurnSafePilotCallbackResult;
using ov_msckf::TurnSafePilotGroupInput;
using ov_msckf::TurnSafePilotMemberInput;
using ov_msckf::TurnSafePilotPairPrior;
using ov_msckf::TurnSafePilotShadow;

constexpr std::size_t kMaximumJsonlRecordBytes =
    static_cast<std::size_t>(256U) * 1024U * 1024U;
constexpr double kRotationAuditTolerance = 1.0e-12;
constexpr double kValueAuditTolerance = 1.0e-12;
constexpr std::uint64_t kFnvOffsetBasis = UINT64_C(1469598103934665603);
constexpr std::uint64_t kFnvPrime = UINT64_C(1099511628211);

class ScanError final : public std::runtime_error {
public:
  explicit ScanError(const std::string &message) : std::runtime_error(message) {}
};

struct Options {
  std::string sequence;
  std::string run;
  std::string archive;
  std::string expected_archive_sha256;
  std::uint64_t expected_archive_size = 0U;
  std::string expected_raw_sha256;
  std::uint64_t expected_raw_size = 0U;
  std::uint64_t expected_record_count = 0U;
  std::string callback_csv;
  std::string metadata_json;
};

struct FileIdentity {
  std::string sha256;
  std::uint64_t size = 0U;
};

struct MatrixValue {
  std::size_t rows = 0U;
  std::size_t cols = 0U;
  std::vector<double> values;
};

struct ObservationKey {
  std::size_t camera_id = 0U;
  double timestamp = 0.0;
  std::string timestamp_key;
  std::uint64_t feature_id = 0U;
  std::uint64_t detached_index = 0U;
  std::size_t observation_ordinal = 0U;
};

struct CameraPrimitive {
  std::size_t camera_id = 0U;
  std::string model;
  std::int64_t extrinsic_id = -1;
  std::int64_t intrinsic_id = -1;
  int width = 0;
  int height = 0;
  MatrixValue extrinsic_value;
  MatrixValue extrinsic_fej;
  MatrixValue intrinsic_value;
  MatrixValue intrinsic_fej;
  MatrixValue projection_cache;
  TurnSafeCertificateCamera certificate;
};

struct ClonePrimitive {
  double timestamp = 0.0;
  std::string timestamp_key;
  std::int64_t covariance_id = -1;
  MatrixValue nominal_pose;
  MatrixValue fej_pose;
  Eigen::Matrix3d current_R_GtoI = Eigen::Matrix3d::Identity();
  Eigen::Matrix3d fej_R_GtoI = Eigen::Matrix3d::Identity();
  Eigen::Vector3d current_position_G = Eigen::Vector3d::Zero();
};

struct PairCovariancePrimitive {
  std::string source_timestamp_key;
  std::string target_timestamp_key;
  std::int64_t source_covariance_id = -1;
  std::int64_t target_covariance_id = -1;
  ov_msckf::TurnSafeCapturedPairCovariance covariance;
};

struct TimestampPairKey {
  std::string source;
  std::string target;
};

struct TimestampPairLess {
  bool operator()(const TimestampPairKey &left,
                  const TimestampPairKey &right) const noexcept {
    if (left.source != right.source) return left.source < right.source;
    return left.target < right.target;
  }
};

struct MemberKeyLess {
  bool operator()(const TurnSafePairMemberKey &left,
                  const TurnSafePairMemberKey &right) const noexcept {
    return ov_msckf::turnsafe_pair_member_key_less(left, right);
  }
};

struct GroupKeyLess {
  bool operator()(const TurnSafePairGroupKey &left,
                  const TurnSafePairGroupKey &right) const noexcept {
    return ov_msckf::turnsafe_pair_group_key_less(left, right);
  }
};

struct BaseCandidate {
  TurnSafePairMemberKey key;
  ObservationKey source_key;
  ObservationKey target_key;
  ObservationKey stereo_key;
  bool source_raw_available = false;
  bool source_normalized_available = false;
  bool target_raw_available = false;
  bool target_normalized_available = false;
  bool stereo_raw_available = false;
  bool stereo_normalized_available = false;
  Eigen::Vector2d source_raw = Eigen::Vector2d::Zero();
  Eigen::Vector2d source_normalized = Eigen::Vector2d::Zero();
  Eigen::Vector2d target_raw = Eigen::Vector2d::Zero();
  Eigen::Vector2d target_normalized = Eigen::Vector2d::Zero();
  Eigen::Vector2d stereo_raw = Eigen::Vector2d::Zero();
  Eigen::Vector2d stereo_normalized = Eigen::Vector2d::Zero();
  std::string full_outcome;
};

struct PriorPrimitives {
  std::map<std::size_t, CameraPrimitive> cameras;
  std::map<std::string, ClonePrimitive> clones;
  std::map<TimestampPairKey, PairCovariancePrimitive, TimestampPairLess> pairs;
};

struct HeaderIdentity {
  std::string frozen_base_sha;
  std::string source_sha;
  std::string tree_sha;
  std::string source_snapshot_sha256;
  std::string build_provenance_id;
  std::string config_sha256;
  std::string calibration_sha256;
};

struct Aggregate {
  std::uint64_t callback_rows = 0U;
  std::uint64_t winner_callbacks = 0U;
  std::uint64_t nis_passing_callbacks = 0U;
  std::uint64_t accepted_winner_callbacks = 0U;
  std::uint64_t winner_nis_evaluated_count = 0U;
  std::uint64_t winner_nis_pass_count = 0U;
  std::uint64_t winner_information_non_negligible_count = 0U;
  std::uint64_t provenance_available_callbacks = 0U;
  std::uint64_t provenance_group_count = 0U;
  std::uint64_t provenance_member_count = 0U;
  std::map<std::string, std::uint64_t> rejection_reasons;
};

std::string usage() {
  return
      "Usage: turnsafe_p1_shadow_worker --sequence VALUE --run VALUE "
      "--archive PATH --expected-archive-sha256 HEX "
      "--expected-archive-size BYTES --expected-raw-sha256 HEX "
      "--expected-raw-size BYTES --expected-record-count COUNT "
      "--output-csv PATH --output-metadata PATH\n";
}

std::uint64_t parse_u64(const std::string &text, const char *label) {
  if (text.empty() ||
      !std::all_of(text.begin(), text.end(), [](unsigned char value) {
        return value >= static_cast<unsigned char>('0') &&
               value <= static_cast<unsigned char>('9');
      })) {
    throw ScanError(std::string(label) + " is not an unsigned decimal integer");
  }
  std::size_t consumed = 0U;
  unsigned long long value = 0U;
  try {
    value = std::stoull(text, &consumed, 10);
  } catch (const std::exception &) {
    throw ScanError(std::string(label) + " is outside uint64 range");
  }
  if (consumed != text.size()) {
    throw ScanError(std::string(label) + " is not canonical decimal");
  }
  return static_cast<std::uint64_t>(value);
}

std::string checked_sha256(std::string value, const char *label) {
  if (value.size() != 64U ||
      !std::all_of(value.begin(), value.end(), [](unsigned char byte) {
        return (byte >= static_cast<unsigned char>('0') &&
                byte <= static_cast<unsigned char>('9')) ||
               (byte >= static_cast<unsigned char>('a') &&
                byte <= static_cast<unsigned char>('f'));
      })) {
    throw ScanError(std::string(label) +
                    " must be 64 lowercase hexadecimal characters");
  }
  return value;
}

Options parse_options(int argc, char **argv) {
  Options output;
  std::map<std::string, std::string *> strings = {
      {"--sequence", &output.sequence},
      {"--run", &output.run},
      {"--archive", &output.archive},
      {"--expected-archive-sha256", &output.expected_archive_sha256},
      {"--expected-raw-sha256", &output.expected_raw_sha256},
      {"--output-csv", &output.callback_csv},
      {"--callback-csv", &output.callback_csv},
      {"--output-metadata", &output.metadata_json},
      {"--metadata-json", &output.metadata_json},
  };
  std::set<std::string> seen;
  std::string archive_size;
  std::string raw_size;
  std::string record_count;
  for (int index = 1; index < argc; ++index) {
    const std::string key(argv[index]);
    if (key == "--help" || key == "-h") {
      std::cout << usage();
      std::exit(0);
    }
    if (index + 1 >= argc) {
      throw ScanError("missing value for CLI argument " + key);
    }
    const std::string value(argv[++index]);
    const auto string_option = strings.find(key);
    if (string_option != strings.end()) {
      const std::string canonical =
          key == "--callback-csv" ? "--output-csv" :
          (key == "--metadata-json" ? "--output-metadata" : key);
      if (!seen.insert(canonical).second) {
        throw ScanError("duplicate CLI argument " + canonical);
      }
      *string_option->second = value;
    } else if (key == "--expected-archive-size") {
      if (!seen.insert(key).second) throw ScanError("duplicate CLI argument " + key);
      archive_size = value;
    } else if (key == "--expected-raw-size") {
      if (!seen.insert(key).second) throw ScanError("duplicate CLI argument " + key);
      raw_size = value;
    } else if (key == "--expected-record-count") {
      if (!seen.insert(key).second) throw ScanError("duplicate CLI argument " + key);
      record_count = value;
    } else {
      throw ScanError("unknown CLI argument " + key);
    }
  }
  const std::array<std::pair<const char *, const std::string *>, 8U> required{{
      {"--sequence", &output.sequence},
      {"--run", &output.run},
      {"--archive", &output.archive},
      {"--expected-archive-sha256", &output.expected_archive_sha256},
      {"--expected-raw-sha256", &output.expected_raw_sha256},
      {"--output-csv", &output.callback_csv},
      {"--output-metadata", &output.metadata_json},
      {"--expected-archive-size", &archive_size},
  }};
  for (const auto &entry : required) {
    if (entry.second->empty()) throw ScanError(std::string("missing ") + entry.first);
  }
  if (raw_size.empty()) throw ScanError("missing --expected-raw-size");
  if (record_count.empty()) throw ScanError("missing --expected-record-count");
  output.expected_archive_sha256 = checked_sha256(
      output.expected_archive_sha256, "expected archive SHA-256");
  output.expected_raw_sha256 = checked_sha256(
      output.expected_raw_sha256, "expected raw SHA-256");
  output.expected_archive_size = parse_u64(archive_size, "expected archive size");
  output.expected_raw_size = parse_u64(raw_size, "expected raw size");
  output.expected_record_count = parse_u64(record_count, "expected record count");
  if (output.expected_archive_size == 0U || output.expected_raw_size == 0U ||
      output.expected_record_count < 2U) {
    throw ScanError("expected sizes must be positive and record count must include header plus callback");
  }
  if (output.archive == output.callback_csv ||
      output.archive == output.metadata_json ||
      output.callback_csv == output.metadata_json) {
    throw ScanError("archive and output paths must be distinct");
  }
  return output;
}

void checked_add(std::uint64_t &value, std::size_t increment,
                 const char *label) {
  if (increment > std::numeric_limits<std::uint64_t>::max() - value) {
    throw ScanError(std::string(label) + " byte count overflow");
  }
  value += static_cast<std::uint64_t>(increment);
}

class Sha256 final {
public:
  Sha256() : context_(EVP_MD_CTX_new(), &EVP_MD_CTX_free) {
    if (!context_ || EVP_DigestInit_ex(context_.get(), EVP_sha256(), nullptr) != 1)
      throw ScanError("OpenSSL SHA-256 initialization failed");
  }

  void update(const void *data, std::size_t size) {
    if (finished_) throw ScanError("SHA-256 update after finalization");
    if (size != 0U && EVP_DigestUpdate(context_.get(), data, size) != 1)
      throw ScanError("OpenSSL SHA-256 update failed");
  }

  std::string finish() {
    if (finished_) throw ScanError("SHA-256 finalized twice");
    std::array<unsigned char, EVP_MAX_MD_SIZE> digest{};
    unsigned int size = 0U;
    if (EVP_DigestFinal_ex(context_.get(), digest.data(), &size) != 1 ||
        size != 32U) {
      throw ScanError("OpenSSL SHA-256 finalization failed");
    }
    finished_ = true;
    std::ostringstream output;
    output << std::hex << std::setfill('0');
    for (unsigned int index = 0U; index < size; ++index)
      output << std::setw(2) << static_cast<unsigned int>(digest[index]);
    return output.str();
  }

private:
  std::unique_ptr<EVP_MD_CTX, decltype(&EVP_MD_CTX_free)> context_;
  bool finished_ = false;
};

std::string parent_directory(const std::string &path) {
  const std::size_t slash = path.find_last_of('/');
  if (slash == std::string::npos) return ".";
  if (slash == 0U) return "/";
  return path.substr(0U, slash);
}

void require_regular_archive(const Options &options) {
  struct stat status {};
  if (lstat(options.archive.c_str(), &status) != 0)
    throw ScanError("archive lstat failed: " + std::string(std::strerror(errno)));
  if (!S_ISREG(status.st_mode) || S_ISLNK(status.st_mode))
    throw ScanError("archive must be a regular non-symlink file");
  if (status.st_size < 0 ||
      static_cast<std::uint64_t>(status.st_size) !=
          options.expected_archive_size) {
    throw ScanError("archive size differs from expected identity");
  }
  for (const std::string *output : {&options.callback_csv,
                                    &options.metadata_json}) {
    struct stat existing {};
    if (lstat(output->c_str(), &existing) == 0)
      throw ScanError("refusing to overwrite existing output " + *output);
    if (errno != ENOENT)
      throw ScanError("output lstat failed for " + *output);
    struct stat directory {};
    const std::string parent = parent_directory(*output);
    if (stat(parent.c_str(), &directory) != 0 || !S_ISDIR(directory.st_mode))
      throw ScanError("output parent directory is unavailable for " + *output);
  }
}

std::string temporary_path(const std::string &final_path) {
  std::ostringstream output;
  output << parent_directory(final_path) << "/."
         << final_path.substr(final_path.find_last_of('/') == std::string::npos
                                  ? 0U
                                  : final_path.find_last_of('/') + 1U)
         << "." << static_cast<unsigned long long>(getpid()) << ".tmp";
  return output.str();
}

class TemporaryOutputs final {
public:
  TemporaryOutputs(const std::string &csv_final,
                   const std::string &metadata_final)
      : csv_final_(csv_final), metadata_final_(metadata_final),
        csv_temporary_(temporary_path(csv_final)),
        metadata_temporary_(temporary_path(metadata_final)) {
    struct stat status {};
    if (lstat(csv_temporary_.c_str(), &status) == 0 || errno != ENOENT)
      throw ScanError("CSV temporary path already exists");
    if (lstat(metadata_temporary_.c_str(), &status) == 0 || errno != ENOENT)
      throw ScanError("metadata temporary path already exists");
  }

  ~TemporaryOutputs() {
    if (!published_) {
      std::remove(csv_temporary_.c_str());
      std::remove(metadata_temporary_.c_str());
    }
  }

  const std::string &csv_temporary() const noexcept { return csv_temporary_; }
  const std::string &metadata_temporary() const noexcept {
    return metadata_temporary_;
  }

  void publish() {
    if (std::rename(csv_temporary_.c_str(), csv_final_.c_str()) != 0)
      throw ScanError("atomic CSV publication failed: " +
                      std::string(std::strerror(errno)));
    if (std::rename(metadata_temporary_.c_str(), metadata_final_.c_str()) != 0) {
      std::remove(csv_final_.c_str());
      throw ScanError("atomic metadata publication failed: " +
                      std::string(std::strerror(errno)));
    }
    published_ = true;
  }

private:
  std::string csv_final_;
  std::string metadata_final_;
  std::string csv_temporary_;
  std::string metadata_temporary_;
  bool published_ = false;
};

const Json::Value &member(const Json::Value &object, const char *name,
                          const std::string &label) {
  if (!object.isObject()) throw ScanError(label + " is not an object");
  if (!object.isMember(name))
    throw ScanError(label + " is missing required field " + name);
  return object[name];
}

const Json::Value &object_member(const Json::Value &object, const char *name,
                                 const std::string &label) {
  const Json::Value &value = member(object, name, label);
  if (!value.isObject()) throw ScanError(label + "." + name + " is not an object");
  return value;
}

const Json::Value &array_member(const Json::Value &object, const char *name,
                                const std::string &label) {
  const Json::Value &value = member(object, name, label);
  if (!value.isArray()) throw ScanError(label + "." + name + " is not an array");
  return value;
}

std::string string_value(const Json::Value &value, const std::string &label) {
  if (!value.isString()) throw ScanError(label + " is not a string");
  return value.asString();
}

std::string string_member(const Json::Value &object, const char *name,
                          const std::string &label) {
  return string_value(member(object, name, label), label + "." + name);
}

bool bool_member(const Json::Value &object, const char *name,
                 const std::string &label) {
  const Json::Value &value = member(object, name, label);
  if (!value.isBool()) throw ScanError(label + "." + name + " is not Boolean");
  return value.asBool();
}

double finite_number(const Json::Value &value, const std::string &label) {
  if (!value.isNumeric() || value.isBool())
    throw ScanError(label + " is not numeric");
  const double output = value.asDouble();
  if (!std::isfinite(output)) throw ScanError(label + " is nonfinite");
  return output;
}

double finite_number_member(const Json::Value &object, const char *name,
                            const std::string &label) {
  return finite_number(member(object, name, label), label + "." + name);
}

std::uint64_t uint64_value(const Json::Value &value,
                           const std::string &label) {
  if (value.isBool() || !value.isIntegral())
    throw ScanError(label + " is not an integer");
  if (value.isInt64() && value.asInt64() < 0)
    throw ScanError(label + " is negative");
  return value.asUInt64();
}

std::uint64_t uint64_member(const Json::Value &object, const char *name,
                            const std::string &label) {
  return uint64_value(member(object, name, label), label + "." + name);
}

std::size_t size_member(const Json::Value &object, const char *name,
                        const std::string &label) {
  const std::uint64_t value = uint64_member(object, name, label);
  if (value > std::numeric_limits<std::size_t>::max())
    throw ScanError(label + "." + name + " exceeds size_t");
  return static_cast<std::size_t>(value);
}

std::int64_t int64_member(const Json::Value &object, const char *name,
                          const std::string &label) {
  const Json::Value &value = member(object, name, label);
  if (value.isBool() || !value.isIntegral())
    throw ScanError(label + "." + name + " is not an integer");
  if (!value.isInt64() && value.asUInt64() >
                              static_cast<std::uint64_t>(
                                  std::numeric_limits<std::int64_t>::max()))
    throw ScanError(label + "." + name + " exceeds int64");
  return value.asInt64();
}

void require_status(const Json::Value &value, const std::string &label,
                    const std::string &expected_status,
                    const std::string &expected_reason) {
  if (!value.isObject()) throw ScanError(label + " is not a typed object");
  if (string_member(value, "status", label) != expected_status ||
      string_member(value, "reason", label) != expected_reason)
    throw ScanError(label + " status/reason differs from required value");
}

std::string timestamp_key(double value) {
  std::uint64_t bits = 0U;
  static_assert(sizeof(bits) == sizeof(value), "binary64 required");
  std::memcpy(&bits, &value, sizeof(bits));
  std::ostringstream output;
  output << "f64:0x" << std::hex << std::setw(16) << std::setfill('0')
         << bits;
  return output.str();
}

void require_timestamp_key(const std::string &key, double value,
                           const std::string &label) {
  if (key != timestamp_key(value))
    throw ScanError(label + " timestamp key differs from binary64 value");
}

bool same_double(double left, double right) noexcept {
  std::uint64_t left_bits = 0U;
  std::uint64_t right_bits = 0U;
  std::memcpy(&left_bits, &left, sizeof(left_bits));
  std::memcpy(&right_bits, &right, sizeof(right_bits));
  return left_bits == right_bits;
}

void inspect_recursive(const Json::Value &value, const std::string &label) {
  static const std::set<std::string> forbidden = {
      "bag", "bag_path", "bag_sha256", "dataset", "event_id",
      "final_error", "ground_truth", "holdout", "outcome_label",
      "path_gt", "sequence", "sequence_id", "sequence_name", "severity"};
  if (value.isDouble() && !std::isfinite(value.asDouble()))
    throw ScanError(label + " contains a nonfinite number");
  if (value.isObject()) {
    for (const std::string &name : value.getMemberNames()) {
      std::string lowered(name);
      std::transform(lowered.begin(), lowered.end(), lowered.begin(),
                     [](unsigned char byte) {
                       return static_cast<char>(std::tolower(byte));
                     });
      if (forbidden.count(lowered) != 0U)
        throw ScanError(label + " contains forbidden runtime key " + name);
      inspect_recursive(value[name], label + "." + name);
    }
  } else if (value.isArray()) {
    for (Json::ArrayIndex index = 0U; index < value.size(); ++index)
      inspect_recursive(value[index], label + "[" +
                                      std::to_string(index) + "]");
  }
}

Json::Value parse_json_line(const std::string &line,
                            std::uint64_t line_number) {
  if (line.empty()) throw ScanError("JSONL line " + std::to_string(line_number) + " is empty");
  Json::CharReaderBuilder builder;
  builder["allowComments"] = false;
  builder["allowDroppedNullPlaceholders"] = false;
  builder["allowNumericKeys"] = false;
  builder["allowSingleQuotes"] = false;
  builder["allowSpecialFloats"] = false;
  builder["collectComments"] = false;
  builder["failIfExtra"] = true;
  builder["rejectDupKeys"] = true;
  builder["skipBom"] = false;
  builder["strictRoot"] = true;
  std::unique_ptr<Json::CharReader> reader(builder.newCharReader());
  Json::Value root;
  std::string errors;
  if (!reader || !reader->parse(line.data(), line.data() + line.size(),
                                &root, &errors)) {
    throw ScanError("JSONL line " + std::to_string(line_number) +
                    " failed strict JSON parsing");
  }
  if (!root.isObject())
    throw ScanError("JSONL line " + std::to_string(line_number) +
                    " is not an object");
  inspect_recursive(root, "record[" + std::to_string(line_number - 1U) + "]");
  return root;
}

MatrixValue parse_matrix(const Json::Value &value, std::size_t expected_rows,
                         std::size_t expected_cols, const char *values_name,
                         const std::string &label) {
  if (!value.isObject()) throw ScanError(label + " is not a matrix object");
  if (value.isMember("status")) {
    if (string_member(value, "status", label) != "AVAILABLE" ||
        (value.isMember("reason") &&
         string_member(value, "reason", label) != "NONE"))
      throw ScanError(label + " matrix status is unavailable");
  }
  MatrixValue output;
  output.rows = size_member(value, "rows", label);
  output.cols = size_member(value, "cols", label);
  if (output.rows != expected_rows || output.cols != expected_cols)
    throw ScanError(label + " dimensions differ from contract");
  const Json::Value &values = array_member(value, values_name, label);
  if (values.size() != expected_rows * expected_cols)
    throw ScanError(label + " element count differs from dimensions");
  output.values.reserve(values.size());
  for (Json::ArrayIndex index = 0U; index < values.size(); ++index)
    output.values.push_back(finite_number(
        values[index], label + "." + values_name + "[" +
                           std::to_string(index) + "]"));
  return output;
}

template <int Rows, int Cols>
Eigen::Matrix<double, Rows, Cols> eigen_matrix(const MatrixValue &value) {
  Eigen::Matrix<double, Rows, Cols> output;
  for (int row = 0; row < Rows; ++row)
    for (int col = 0; col < Cols; ++col)
      output(row, col) = value.values[static_cast<std::size_t>(row * Cols + col)];
  return output;
}

Eigen::Matrix3d skew(const Eigen::Vector3d &value) {
  Eigen::Matrix3d output;
  output << 0.0, -value.z(), value.y(), value.z(), 0.0, -value.x(),
      -value.y(), value.x(), 0.0;
  return output;
}

void pose_from_jpl(const MatrixValue &pose, Eigen::Matrix3d &rotation,
                   Eigen::Vector3d &position, const std::string &label) {
  if (pose.rows != 7U || pose.cols != 1U || pose.values.size() != 7U)
    throw ScanError(label + " is not a 7x1 JPL pose");
  Eigen::Vector4d quaternion(pose.values[0], pose.values[1], pose.values[2],
                             pose.values[3]);
  const double norm = quaternion.norm();
  if (!std::isfinite(norm) || std::fabs(norm - 1.0) > 1.0e-10)
    throw ScanError(label + " quaternion is not unit");
  quaternion /= norm;
  const Eigen::Vector3d vector = quaternion.head<3>();
  const double scalar = quaternion(3);
  rotation = (2.0 * std::pow(scalar, 2) - 1.0) *
                 Eigen::Matrix3d::Identity() -
             2.0 * scalar * skew(vector) +
             2.0 * vector * vector.transpose();
  position = Eigen::Vector3d(pose.values[4], pose.values[5], pose.values[6]);
  if (!rotation.allFinite() || !position.allFinite() ||
      (rotation * rotation.transpose() - Eigen::Matrix3d::Identity())
              .cwiseAbs().maxCoeff() > 1.0e-10 ||
      std::fabs(rotation.determinant() - 1.0) > 1.0e-10)
    throw ScanError(label + " converts to an invalid repository rotation");
}

void require_matrix_near(const Eigen::Matrix3d &actual,
                         const Eigen::Matrix3d &expected,
                         const std::string &label) {
  if (!actual.allFinite() || !expected.allFinite() ||
      (actual - expected).cwiseAbs().maxCoeff() > kRotationAuditTolerance)
    throw ScanError(label + " differs from captured JPL pose");
}

void fnv_mix(std::uint64_t &hash, unsigned char byte) noexcept {
  hash ^= static_cast<std::uint64_t>(byte);
  hash *= kFnvPrime;
}

void fnv_mix_u64_be(std::uint64_t &hash, std::uint64_t value) noexcept {
  for (int shift = 56; shift >= 0; shift -= 8)
    fnv_mix(hash, static_cast<unsigned char>((value >> shift) & UINT64_C(0xff)));
}

std::string canonical_matrix_hash(const MatrixValue &matrix) {
  std::uint64_t hash = kFnvOffsetBasis;
  fnv_mix_u64_be(hash, static_cast<std::uint64_t>(matrix.rows));
  fnv_mix_u64_be(hash, static_cast<std::uint64_t>(matrix.cols));
  for (double value : matrix.values) {
    std::uint64_t bits = 0U;
    std::memcpy(&bits, &value, sizeof(bits));
    fnv_mix_u64_be(hash, bits);
  }
  std::ostringstream output;
  output << "fnv1a64:" << std::hex << std::setw(16) << std::setfill('0')
         << hash;
  return output.str();
}

TurnSafePairGroupKey parse_group_key(const Json::Value &value,
                                     const std::string &label) {
  TurnSafePairGroupKey output;
  output.camera_id = size_member(value, "camera_id", label);
  output.source_timestamp_key = string_member(value, "source_timestamp_key", label);
  output.target_timestamp_key = string_member(value, "target_timestamp_key", label);
  if (output.source_timestamp_key == output.target_timestamp_key)
    throw ScanError(label + " has identical source and target timestamps");
  return output;
}

TurnSafePairMemberKey parse_member_key(const Json::Value &value,
                                       const std::string &label) {
  TurnSafePairMemberKey output;
  output.camera_id = size_member(value, "camera_id", label);
  output.source_timestamp_key = string_member(value, "source_timestamp_key", label);
  output.target_timestamp_key = string_member(value, "target_timestamp_key", label);
  output.feature_id = uint64_member(value, "feature_id", label);
  output.detached_index = uint64_member(value, "detached_index", label);
  output.source_observation_ordinal =
      size_member(value, "source_observation_ordinal", label);
  output.target_observation_ordinal =
      size_member(value, "target_observation_ordinal", label);
  if (output.source_timestamp_key == output.target_timestamp_key)
    throw ScanError(label + " has identical source and target timestamps");
  return output;
}

ObservationKey parse_observation_key(const Json::Value &value,
                                     const std::string &label) {
  ObservationKey output;
  output.camera_id = size_member(value, "camera_id", label);
  output.timestamp = finite_number_member(value, "timestamp_value", label);
  output.timestamp_key = string_member(value, "timestamp_key", label);
  require_timestamp_key(output.timestamp_key, output.timestamp, label);
  output.feature_id = uint64_member(value, "feature_id", label);
  output.detached_index = uint64_member(value, "detached_index", label);
  output.observation_ordinal = size_member(value, "observation_ordinal", label);
  return output;
}

bool observation_key_equal(const ObservationKey &left,
                           const ObservationKey &right) noexcept {
  return left.camera_id == right.camera_id &&
         same_double(left.timestamp, right.timestamp) &&
         left.timestamp_key == right.timestamp_key &&
         left.feature_id == right.feature_id &&
         left.detached_index == right.detached_index &&
         left.observation_ordinal == right.observation_ordinal;
}

void audit_observation_member(const ObservationKey &observation,
                              const TurnSafePairMemberKey &member_key,
                              bool source, const std::string &label) {
  if (observation.camera_id != member_key.camera_id ||
      observation.timestamp_key !=
          (source ? member_key.source_timestamp_key
                  : member_key.target_timestamp_key) ||
      observation.feature_id != member_key.feature_id ||
      observation.detached_index != member_key.detached_index ||
      observation.observation_ordinal !=
          (source ? member_key.source_observation_ordinal
                  : member_key.target_observation_ordinal))
    throw ScanError(label + " does not bind to member key");
}

bool eligible_outcome(const std::string &outcome) {
  static const std::set<std::string> eligible = {
      "INIT_ILL_CONDITIONED", "INIT_TOO_FAR", "INIT_BASELINE_RATIO",
      "SCHUR_RANK_DEFICIENT", "SCHUR_ILL_CONDITIONED"};
  return eligible.count(outcome) != 0U;
}

bool optional_vec2(const Json::Value &value, Eigen::Vector2d &output,
                   const std::string &label) {
  if (value.isArray()) {
    if (value.size() != 2U) throw ScanError(label + " must contain two values");
    output << finite_number(value[0U], label + "[0]"),
              finite_number(value[1U], label + "[1]");
    return true;
  }
  if (!value.isObject()) throw ScanError(label + " is neither vector nor typed status");
  const std::string status = string_member(value, "status", label);
  const std::string reason = string_member(value, "reason", label);
  if (status == "AVAILABLE") {
    if (reason != "NONE") throw ScanError(label + " available reason is not NONE");
    const Json::Value &vector = array_member(value, "value", label);
    if (vector.size() != 2U) throw ScanError(label + " must contain two values");
    output << finite_number(vector[0U], label + ".value[0]"),
              finite_number(vector[1U], label + ".value[1]");
    return true;
  }
  if (reason.empty() || value.isMember("value"))
    throw ScanError(label + " unavailable status is malformed");
  return false;
}

bool typed_vec(const Json::Value &value, std::size_t dimensions,
               std::vector<double> &output, const std::string &label) {
  if (!value.isObject()) throw ScanError(label + " is not a typed vector");
  const std::string status = string_member(value, "status", label);
  const std::string reason = string_member(value, "reason", label);
  if (status != "AVAILABLE") {
    if (reason.empty() || value.isMember("value"))
      throw ScanError(label + " unavailable status is malformed");
    return false;
  }
  if (reason != "NONE") throw ScanError(label + " available reason is not NONE");
  const Json::Value &raw = array_member(value, "value", label);
  if (raw.size() != dimensions)
    throw ScanError(label + " vector dimension differs from contract");
  output.clear();
  output.reserve(dimensions);
  for (Json::ArrayIndex index = 0U; index < raw.size(); ++index)
    output.push_back(finite_number(raw[index], label + ".value[" +
                                              std::to_string(index) + "]"));
  return true;
}

void require_vec_equal(bool left_available, const Eigen::Vector2d &left,
                       bool right_available, const std::vector<double> &right,
                       const std::string &label) {
  if (left_available != right_available)
    throw ScanError(label + " availability differs between base and extension");
  if (left_available &&
      (!same_double(left.x(), right[0]) || !same_double(left.y(), right[1])))
    throw ScanError(label + " value differs between base and extension");
}

std::string compact_json(const Json::Value &value) {
  Json::StreamWriterBuilder builder;
  builder["commentStyle"] = "None";
  builder["emitUTF8"] = true;
  builder["indentation"] = "";
  builder["precision"] = 17;
  builder["precisionType"] = "significant";
  std::string output = Json::writeString(builder, value);
  while (!output.empty() && (output.back() == '\n' || output.back() == '\r'))
    output.pop_back();
  return output;
}

std::string numeric(double value) {
  if (!std::isfinite(value)) return "";
  std::ostringstream output;
  output.imbue(std::locale::classic());
  output << std::setprecision(std::numeric_limits<double>::max_digits10)
         << value;
  return output.str();
}

std::string boolean(bool value) { return value ? "true" : "false"; }

std::string csv_escape(const std::string &value) {
  if (value.find_first_of(",\"\r\n") == std::string::npos) return value;
  std::string output = "\"";
  for (char byte : value) {
    if (byte == '\"') output += '\"';
    output += byte;
  }
  output += '\"';
  return output;
}

FileIdentity file_identity(const std::string &path) {
  std::ifstream input(path, std::ios::binary);
  if (!input) throw ScanError("cannot open file for output identity: " + path);
  Sha256 digest;
  FileIdentity output;
  std::array<char, 1024U * 1024U> buffer{};
  while (input) {
    input.read(buffer.data(), static_cast<std::streamsize>(buffer.size()));
    const std::streamsize count = input.gcount();
    if (count > 0) {
      digest.update(buffer.data(), static_cast<std::size_t>(count));
      checked_add(output.size, static_cast<std::size_t>(count), "file identity");
    }
  }
  if (!input.eof()) throw ScanError("file identity read failed: " + path);
  output.sha256 = digest.finish();
  return output;
}

bool matrix_bits_equal(const MatrixValue &left,
                       const MatrixValue &right) noexcept {
  if (left.rows != right.rows || left.cols != right.cols ||
      left.values.size() != right.values.size())
    return false;
  for (std::size_t index = 0U; index < left.values.size(); ++index)
    if (!same_double(left.values[index], right.values[index])) return false;
  return true;
}

double covariance_symmetry_bound(const Eigen::MatrixXd &matrix) {
  const double scale = std::max(1.0, matrix.cwiseAbs().maxCoeff());
  return 64.0 * std::numeric_limits<double>::epsilon() *
         static_cast<double>(std::max<Eigen::Index>(1, matrix.rows())) * scale;
}

void audit_pair_covariance(
    const ov_msckf::TurnSafeCapturedPairCovariance &covariance,
    const std::string &label) {
  Eigen::Matrix<double, 12, 12> combined;
  combined << covariance.P_ss, covariance.P_st,
              covariance.P_ts, covariance.P_tt;
  if (!combined.allFinite()) throw ScanError(label + " is nonfinite");
  const double error =
      (combined - combined.transpose()).cwiseAbs().maxCoeff();
  if (!std::isfinite(error) || error > covariance_symmetry_bound(combined))
    throw ScanError(label + " is not symmetric within audited roundoff");
}

PriorPrimitives parse_prior_primitives(const Json::Value &value,
                                       bool required,
                                       const std::string &label) {
  PriorPrimitives output;
  if (!value.isObject()) throw ScanError(label + " is not an object");
  const std::string status = string_member(value, "status", label);
  const std::string reason = string_member(value, "reason", label);
  if (status != "AVAILABLE") {
    if (required) throw ScanError(label + " is unavailable for provenance groups");
    if (reason.empty()) throw ScanError(label + " unavailable reason is empty");
    return output;
  }
  if (reason != "NONE" && reason != "NO_LIVE_SAME_CAMERA_CLONE_PAIRS")
    throw ScanError(label + " available reason is invalid");
  const std::size_t covariance_dimension =
      size_member(value, "covariance_dimension", label);

  const Json::Value &cameras = array_member(value, "cameras", label);
  std::size_t previous_camera_id = 0U;
  bool have_previous_camera = false;
  for (Json::ArrayIndex index = 0U; index < cameras.size(); ++index) {
    const Json::Value &raw = cameras[index];
    const std::string camera_label = label + ".cameras[" +
                                     std::to_string(index) + "]";
    CameraPrimitive camera;
    camera.camera_id = size_member(raw, "camera_id", camera_label);
    if (have_previous_camera && camera.camera_id <= previous_camera_id)
      throw ScanError(label + " cameras are not strictly canonical");
    previous_camera_id = camera.camera_id;
    have_previous_camera = true;
    camera.model = string_member(raw, "model", camera_label);
    if (camera.model != "RADTAN")
      throw ScanError(camera_label + " is not RADTAN");
    camera.extrinsic_id = int64_member(raw, "extrinsic_id", camera_label);
    camera.intrinsic_id = int64_member(raw, "intrinsic_id", camera_label);
    // Fixed, non-state calibration snapshots use -1 as their repository
    // identity.  Values below that sentinel are malformed; -1 remains bound
    // by the full value/FEJ/cache/hash audits below.
    if (camera.extrinsic_id < -1 || camera.intrinsic_id < -1)
      throw ScanError(camera_label + " calibration identity is below -1");
    const std::uint64_t width = uint64_member(raw, "width", camera_label);
    const std::uint64_t height = uint64_member(raw, "height", camera_label);
    if (width == 0U || height == 0U ||
        width > static_cast<std::uint64_t>(std::numeric_limits<int>::max()) ||
        height > static_cast<std::uint64_t>(std::numeric_limits<int>::max()))
      throw ScanError(camera_label + " image dimensions are invalid");
    camera.width = static_cast<int>(width);
    camera.height = static_cast<int>(height);
    camera.extrinsic_value = parse_matrix(
        object_member(raw, "extrinsic_value", camera_label), 7U, 1U,
        "values", camera_label + ".extrinsic_value");
    camera.extrinsic_fej = parse_matrix(
        object_member(raw, "extrinsic_fej", camera_label), 7U, 1U,
        "values", camera_label + ".extrinsic_fej");
    camera.intrinsic_value = parse_matrix(
        object_member(raw, "intrinsic_value", camera_label), 8U, 1U,
        "values", camera_label + ".intrinsic_value");
    camera.intrinsic_fej = parse_matrix(
        object_member(raw, "intrinsic_fej", camera_label), 8U, 1U,
        "values", camera_label + ".intrinsic_fej");
    camera.projection_cache = parse_matrix(
        object_member(raw, "projection_cache", camera_label), 8U, 1U,
        "values", camera_label + ".projection_cache");
    if (!matrix_bits_equal(camera.extrinsic_value, camera.extrinsic_fej) ||
        !matrix_bits_equal(camera.intrinsic_value, camera.intrinsic_fej) ||
        !matrix_bits_equal(camera.intrinsic_value, camera.projection_cache))
      throw ScanError(camera_label + " fixed calibration snapshots differ");
    Eigen::Vector3d unused_position;
    pose_from_jpl(camera.extrinsic_value, camera.certificate.R_ItoC,
                  camera.certificate.p_IinC,
                  camera_label + ".extrinsic_value");
    Eigen::Matrix3d fej_rotation;
    pose_from_jpl(camera.extrinsic_fej, fej_rotation, unused_position,
                  camera_label + ".extrinsic_fej");
    require_matrix_near(fej_rotation, camera.certificate.R_ItoC,
                        camera_label + " FEJ extrinsic rotation");
    camera.certificate.camera_id = camera.camera_id;
    camera.certificate.width = camera.width;
    camera.certificate.height = camera.height;
    camera.certificate.intrinsics =
        eigen_matrix<8, 1>(camera.intrinsic_value);
    if (!output.cameras.emplace(camera.camera_id, camera).second)
      throw ScanError(label + " contains a duplicate camera");
  }

  const Json::Value &clones = array_member(value, "clones", label);
  std::string previous_clone_key;
  for (Json::ArrayIndex index = 0U; index < clones.size(); ++index) {
    const Json::Value &raw = clones[index];
    const std::string clone_label = label + ".clones[" +
                                    std::to_string(index) + "]";
    ClonePrimitive clone;
    clone.timestamp = finite_number_member(raw, "timestamp_value", clone_label);
    clone.timestamp_key = string_member(raw, "timestamp_key", clone_label);
    require_timestamp_key(clone.timestamp_key, clone.timestamp, clone_label);
    if (!previous_clone_key.empty() && clone.timestamp_key <= previous_clone_key)
      throw ScanError(label + " clone keys are not strictly canonical");
    previous_clone_key = clone.timestamp_key;
    clone.covariance_id = int64_member(raw, "covariance_id", clone_label);
    if (clone.covariance_id < 0 ||
        static_cast<std::uint64_t>(clone.covariance_id) + 6U >
            covariance_dimension)
      throw ScanError(clone_label + " covariance block is outside prior");
    clone.nominal_pose = parse_matrix(
        object_member(raw, "nominal_pose", clone_label), 7U, 1U,
        "values", clone_label + ".nominal_pose");
    clone.fej_pose = parse_matrix(
        object_member(raw, "fej_pose", clone_label), 7U, 1U,
        "values", clone_label + ".fej_pose");
    pose_from_jpl(clone.nominal_pose, clone.current_R_GtoI,
                  clone.current_position_G, clone_label + ".nominal_pose");
    Eigen::Vector3d fej_position;
    pose_from_jpl(clone.fej_pose, clone.fej_R_GtoI, fej_position,
                  clone_label + ".fej_pose");
    if (!output.clones.emplace(clone.timestamp_key, clone).second)
      throw ScanError(label + " contains a duplicate clone");
  }

  const Json::Value &pairs = array_member(value, "pair_covariances", label);
  TimestampPairKey previous_pair;
  bool have_previous_pair = false;
  TimestampPairLess pair_less;
  for (Json::ArrayIndex index = 0U; index < pairs.size(); ++index) {
    const Json::Value &raw = pairs[index];
    const std::string pair_label = label + ".pair_covariances[" +
                                   std::to_string(index) + "]";
    TimestampPairKey key{
        string_member(raw, "source_timestamp_key", pair_label),
        string_member(raw, "target_timestamp_key", pair_label)};
    if (key.source == key.target)
      throw ScanError(pair_label + " has identical timestamps");
    if (have_previous_pair && !pair_less(previous_pair, key))
      throw ScanError(label + " pair covariances are not strictly canonical");
    previous_pair = key;
    have_previous_pair = true;
    PairCovariancePrimitive pair;
    pair.source_timestamp_key = key.source;
    pair.target_timestamp_key = key.target;
    pair.source_covariance_id =
        int64_member(raw, "source_covariance_id", pair_label);
    pair.target_covariance_id =
        int64_member(raw, "target_covariance_id", pair_label);
    const auto source_clone = output.clones.find(key.source);
    const auto target_clone = output.clones.find(key.target);
    if (source_clone == output.clones.end() ||
        target_clone == output.clones.end() ||
        source_clone->second.covariance_id != pair.source_covariance_id ||
        target_clone->second.covariance_id != pair.target_covariance_id)
      throw ScanError(pair_label + " covariance IDs do not bind to clones");
    pair.covariance.P_ss = eigen_matrix<6, 6>(parse_matrix(
        object_member(raw, "P_ss", pair_label), 6U, 6U, "values",
        pair_label + ".P_ss"));
    pair.covariance.P_tt = eigen_matrix<6, 6>(parse_matrix(
        object_member(raw, "P_tt", pair_label), 6U, 6U, "values",
        pair_label + ".P_tt"));
    pair.covariance.P_st = eigen_matrix<6, 6>(parse_matrix(
        object_member(raw, "P_st", pair_label), 6U, 6U, "values",
        pair_label + ".P_st"));
    pair.covariance.P_ts = eigen_matrix<6, 6>(parse_matrix(
        object_member(raw, "P_ts", pair_label), 6U, 6U, "values",
        pair_label + ".P_ts"));
    audit_pair_covariance(pair.covariance, pair_label);
    if (!output.pairs.emplace(key, pair).second)
      throw ScanError(label + " contains duplicate pair covariance");
  }
  return output;
}

void audit_camera_identity(const Json::Value &value,
                           const CameraPrimitive &camera,
                           const std::string &label) {
  if (!value.isObject()) throw ScanError(label + " is not an object");
  if (string_member(value, "status", label) != "AVAILABLE")
    throw ScanError(label + " is unavailable");
  if (size_member(value, "camera_id", label) != camera.camera_id ||
      string_member(value, "model", label) != camera.model ||
      int64_member(value, "intrinsic_id", label) != camera.intrinsic_id ||
      int64_member(value, "extrinsic_id", label) != camera.extrinsic_id)
    throw ScanError(label + " differs from captured prior camera");
}

void audit_group_camera(const Json::Value &value,
                        const CameraPrimitive &camera,
                        const std::string &label) {
  require_status(value, label, "AVAILABLE", "NONE");
  if (size_member(value, "camera_id", label) != camera.camera_id ||
      string_member(value, "model", label) != camera.model ||
      uint64_member(value, "image_width", label) !=
          static_cast<std::uint64_t>(camera.width) ||
      uint64_member(value, "image_height", label) !=
          static_cast<std::uint64_t>(camera.height) ||
      string_member(value, "hash_algorithm", label) !=
          "fnv1a64_binary64_be.v1" ||
      string_member(value, "intrinsics_distortion_hash", label) !=
          canonical_matrix_hash(camera.intrinsic_value) ||
      string_member(value, "camera_extrinsic_hash", label) !=
          canonical_matrix_hash(camera.extrinsic_value))
    throw ScanError(label + " hash/dimension identity differs from prior");
}

BaseCandidate parse_base_candidate(const Json::Value &value,
                                   const TurnSafePairMemberKey &expected_key,
                                   const PriorPrimitives &prior,
                                   const std::string &label) {
  BaseCandidate output;
  output.key = parse_member_key(object_member(value, "candidate_key", label),
                                label + ".candidate_key");
  if (!ov_msckf::turnsafe_pair_member_key_equal(output.key, expected_key))
    throw ScanError(label + " candidate key lookup changed identity");
  output.full_outcome = string_member(value, "full_outcome", label);
  if (!eligible_outcome(output.full_outcome))
    throw ScanError(label + " provenance member has ineligible base outcome");
  if (!bool_member(value, "target_time_stereo_available", label))
    throw ScanError(label + " lacks exact target-time stereo evidence");
  if (string_member(value, "target_stereo_rejection_reason", label).empty())
    throw ScanError(label + " has an empty stereo diagnostic reason");

  output.source_key = parse_observation_key(
      object_member(value, "source_observation_key", label),
      label + ".source_observation_key");
  output.target_key = parse_observation_key(
      object_member(value, "target_observation_key", label),
      label + ".target_observation_key");
  output.stereo_key = parse_observation_key(
      object_member(value, "supporting_target_time_stereo_observation_key",
                    label),
      label + ".supporting_target_time_stereo_observation_key");
  audit_observation_member(output.source_key, output.key, true,
                           label + ".source_observation_key");
  audit_observation_member(output.target_key, output.key, false,
                           label + ".target_observation_key");
  if (output.stereo_key.camera_id == output.key.camera_id ||
      output.stereo_key.timestamp_key != output.key.target_timestamp_key ||
      output.stereo_key.feature_id != output.key.feature_id ||
      output.stereo_key.detached_index != output.key.detached_index)
    throw ScanError(label + " stereo observation does not bind to member");

  const Json::Value &source = object_member(value, "source_observation", label);
  const Json::Value &target = object_member(value, "target_observation", label);
  const Json::Value &stereo = object_member(
      value, "supporting_target_time_stereo_observation", label);
  if (!observation_key_equal(
          parse_observation_key(object_member(source, "observation_key",
                                               label + ".source_observation"),
                                label + ".source_observation.observation_key"),
          output.source_key) ||
      !observation_key_equal(
          parse_observation_key(object_member(target, "observation_key",
                                               label + ".target_observation"),
                                label + ".target_observation.observation_key"),
          output.target_key) ||
      !observation_key_equal(
          parse_observation_key(object_member(stereo, "observation_key",
                                               label + ".stereo_observation"),
                                label + ".stereo_observation.observation_key"),
          output.stereo_key))
    throw ScanError(label + " nested observation keys differ");
  output.source_raw_available = optional_vec2(
      member(source, "raw_pixel", label + ".source_observation"),
      output.source_raw, label + ".source_observation.raw_pixel");
  output.source_normalized_available = optional_vec2(
      member(source, "normalized_pixel", label + ".source_observation"),
      output.source_normalized,
      label + ".source_observation.normalized_pixel");
  output.target_raw_available = optional_vec2(
      member(target, "raw_pixel", label + ".target_observation"),
      output.target_raw, label + ".target_observation.raw_pixel");
  output.target_normalized_available = optional_vec2(
      member(target, "normalized_pixel", label + ".target_observation"),
      output.target_normalized,
      label + ".target_observation.normalized_pixel");
  output.stereo_raw_available = optional_vec2(
      member(stereo, "raw_pixel", label + ".stereo_observation"),
      output.stereo_raw, label + ".stereo_observation.raw_pixel");
  output.stereo_normalized_available = optional_vec2(
      member(stereo, "normalized_pixel", label + ".stereo_observation"),
      output.stereo_normalized,
      label + ".stereo_observation.normalized_pixel");

  const auto main_camera = prior.cameras.find(output.key.camera_id);
  const auto stereo_camera = prior.cameras.find(output.stereo_key.camera_id);
  if (main_camera == prior.cameras.end() || stereo_camera == prior.cameras.end())
    throw ScanError(label + " camera is absent from prior primitives");
  const Json::Value &calibration =
      object_member(value, "camera_calibration", label);
  audit_camera_identity(object_member(calibration, "source", label +
                                      ".camera_calibration"),
                        main_camera->second,
                        label + ".camera_calibration.source");
  audit_camera_identity(object_member(calibration, "target", label +
                                      ".camera_calibration"),
                        main_camera->second,
                        label + ".camera_calibration.target");
  audit_camera_identity(object_member(calibration, "stereo", label +
                                      ".camera_calibration"),
                        stereo_camera->second,
                        label + ".camera_calibration.stereo");
  return output;
}

TurnSafeCertificateObservation make_observation(
    const ObservationKey &key, bool raw_available, const Eigen::Vector2d &raw,
    bool normalized_available, const Eigen::Vector2d &normalized) {
  TurnSafeCertificateObservation output;
  output.feature_id = key.feature_id;
  output.detached_index = key.detached_index;
  output.camera_id = key.camera_id;
  output.timestamp = key.timestamp;
  if (raw_available) output.raw_pixel = raw;
  if (normalized_available) output.captured_normalized = normalized;
  return output;
}

void audit_typed_bearing(const Json::Value &value, bool normalized_available,
                         const Eigen::Vector2d &normalized,
                         const std::string &label) {
  std::vector<double> bearing;
  const bool available = typed_vec(value, 3U, bearing, label);
  if (available != normalized_available)
    throw ScanError(label + " availability differs from normalized coordinate");
  if (!available) return;
  if (string_member(value, "frame", label) != "camera")
    throw ScanError(label + " is not in camera frame");
  const Eigen::Vector3d ray(normalized.x(), normalized.y(), 1.0);
  const Eigen::Vector3d expected = ray.normalized();
  const Eigen::Vector3d actual(bearing[0], bearing[1], bearing[2]);
  if ((actual - expected).cwiseAbs().maxCoeff() > kValueAuditTolerance)
    throw ScanError(label + " differs from normalized-coordinate unit ray");
}

void audit_image_cell(const Json::Value &value, bool raw_available,
                      const Eigen::Vector2d &raw, int width, int height,
                      const std::string &label) {
  std::vector<double> cell;
  const bool available = typed_vec(value, 2U, cell, label);
  if (available != raw_available)
    throw ScanError(label + " availability differs from raw pixel");
  if (!available) return;
  if (string_member(value, "representation", label) !=
      "continuous_pixel_center_fraction.v1")
    throw ScanError(label + " representation differs from contract");
  const Eigen::Vector2d expected(
      (raw.x() + 0.5) / static_cast<double>(width),
      (raw.y() + 0.5) / static_cast<double>(height));
  if (!same_double(cell[0], expected.x()) ||
      !same_double(cell[1], expected.y()))
    throw ScanError(label + " differs from pixel-center fraction");
}

TurnSafePilotMemberInput parse_group_member(
    const Json::Value &value, const Json::Value &listed_key,
    const std::map<TurnSafePairMemberKey, const Json::Value *, MemberKeyLess>
        &base_candidates,
    const PriorPrimitives &prior, const TurnSafePairGroupKey &group_key,
    const CameraPrimitive &main_camera,
    const CameraPrimitive &stereo_camera, const std::string &label) {
  TurnSafePilotMemberInput output;
  output.key = parse_member_key(object_member(value, "member_key", label),
                                label + ".member_key");
  const TurnSafePairMemberKey list_key =
      parse_member_key(listed_key, label + ".listed_member_key");
  if (!ov_msckf::turnsafe_pair_member_key_equal(output.key, list_key) ||
      output.key.camera_id != group_key.camera_id ||
      output.key.source_timestamp_key != group_key.source_timestamp_key ||
      output.key.target_timestamp_key != group_key.target_timestamp_key)
    throw ScanError(label + " member/list/group identities differ");
  if (uint64_member(value, "feature_id", label) != output.key.feature_id ||
      uint64_member(value, "detached_index", label) != output.key.detached_index)
    throw ScanError(label + " compact identity differs from member key");
  const auto found = base_candidates.find(output.key);
  if (found == base_candidates.end())
    throw ScanError(label + " has no matching base shadow candidate");
  const BaseCandidate base = parse_base_candidate(
      *found->second, output.key, prior, label + ".base_candidate");
  if (base.stereo_key.camera_id != stereo_camera.camera_id)
    throw ScanError(label + " stereo camera differs from shared group camera");

  const ObservationKey source_key = parse_observation_key(
      object_member(value, "source_observation_key", label),
      label + ".source_observation_key");
  const ObservationKey target_key = parse_observation_key(
      object_member(value, "target_observation_key", label),
      label + ".target_observation_key");
  const ObservationKey stereo_key = parse_observation_key(
      object_member(value, "target_stereo_observation_key", label),
      label + ".target_stereo_observation_key");
  if (!observation_key_equal(source_key, base.source_key) ||
      !observation_key_equal(target_key, base.target_key) ||
      !observation_key_equal(stereo_key, base.stereo_key))
    throw ScanError(label + " observation provenance differs from base candidate");

  std::vector<double> source_raw;
  std::vector<double> target_raw;
  std::vector<double> source_normalized;
  std::vector<double> target_normalized;
  std::vector<double> stereo_raw;
  const bool source_raw_available = typed_vec(
      member(value, "source_raw_pixel", label), 2U, source_raw,
      label + ".source_raw_pixel");
  const bool target_raw_available = typed_vec(
      member(value, "target_raw_pixel", label), 2U, target_raw,
      label + ".target_raw_pixel");
  const bool source_normalized_available = typed_vec(
      member(value, "source_normalized_coordinates", label), 2U,
      source_normalized, label + ".source_normalized_coordinates");
  const bool target_normalized_available = typed_vec(
      member(value, "target_normalized_coordinates", label), 2U,
      target_normalized, label + ".target_normalized_coordinates");
  const bool stereo_raw_available = typed_vec(
      member(value, "target_stereo_raw_pixel", label), 2U, stereo_raw,
      label + ".target_stereo_raw_pixel");
  require_vec_equal(base.source_raw_available, base.source_raw,
                    source_raw_available, source_raw,
                    label + ".source_raw_pixel");
  require_vec_equal(base.target_raw_available, base.target_raw,
                    target_raw_available, target_raw,
                    label + ".target_raw_pixel");
  require_vec_equal(base.source_normalized_available, base.source_normalized,
                    source_normalized_available, source_normalized,
                    label + ".source_normalized_coordinates");
  require_vec_equal(base.target_normalized_available, base.target_normalized,
                    target_normalized_available, target_normalized,
                    label + ".target_normalized_coordinates");
  require_vec_equal(base.stereo_raw_available, base.stereo_raw,
                    stereo_raw_available, stereo_raw,
                    label + ".target_stereo_raw_pixel");

  audit_typed_bearing(member(value, "source_unit_bearing", label),
                      base.source_normalized_available, base.source_normalized,
                      label + ".source_unit_bearing");
  audit_typed_bearing(member(value, "target_unit_bearing", label),
                      base.target_normalized_available, base.target_normalized,
                      label + ".target_unit_bearing");
  audit_image_cell(member(value, "source_image_cell", label),
                   base.source_raw_available, base.source_raw,
                   main_camera.width, main_camera.height,
                   label + ".source_image_cell");
  audit_image_cell(member(value, "target_image_cell", label),
                   base.target_raw_available, base.target_raw,
                   main_camera.width, main_camera.height,
                   label + ".target_image_cell");
  audit_image_cell(member(value, "target_stereo_image_cell", label),
                   base.stereo_raw_available, base.stereo_raw,
                   stereo_camera.width, stereo_camera.height,
                   label + ".target_stereo_image_cell");

  const Json::Value &native = object_member(value, "native_source_status", label);
  require_status(native, label + ".native_source_status", "AVAILABLE",
                 "TYPED_T1_SOURCE_OUTCOME");
  if (string_member(native, "full_outcome",
                    label + ".native_source_status") != base.full_outcome)
    throw ScanError(label + " typed source outcome differs from base candidate");
  const bool finite_member = base.source_raw_available &&
      base.source_normalized_available && base.target_raw_available &&
      base.target_normalized_available && base.stereo_raw_available;
  const Json::Value &finite_validation =
      object_member(value, "finite_validation", label);
  const std::string finite_status = string_member(
      finite_validation, "status", label + ".finite_validation");
  const std::string finite_reason = string_member(
      finite_validation, "reason", label + ".finite_validation");
  if ((finite_member && (finite_status != "AVAILABLE" ||
                         finite_reason != "NONE")) ||
      (!finite_member && finite_status == "AVAILABLE"))
    throw ScanError(label + " finite-validation claim is inconsistent");

  output.stereo_camera_id = stereo_camera.camera_id;
  output.source_observation = make_observation(
      base.source_key, base.source_raw_available, base.source_raw,
      base.source_normalized_available, base.source_normalized);
  output.target_observation = make_observation(
      base.target_key, base.target_raw_available, base.target_raw,
      base.target_normalized_available, base.target_normalized);
  output.target_stereo_observation = make_observation(
      base.stereo_key, base.stereo_raw_available, base.stereo_raw,
      base.stereo_normalized_available, base.stereo_normalized);
  return output;
}

HeaderIdentity validate_header(const Json::Value &record) {
  const std::string label = "run_header";
  if (string_member(record, "schema", label) != "turnsafe.t0.v1" ||
      string_member(record, "record_type", label) != "run_header")
    throw ScanError("first JSONL record is not a TurnSafe T0 run header");
  if (string_member(record, "capture_mode", label) != "capture_only" ||
      !bool_member(record, "supported_configuration", label))
    throw ScanError("run header is not supported capture-only telemetry");
  const Json::Value &unsupported =
      array_member(record, "unsupported_reasons", label);
  if (!unsupported.empty())
    throw ScanError("run header contains unsupported-configuration reasons");
  const Json::Value &resolved =
      object_member(record, "resolved_configuration", label);
  static const std::array<const char *, 9U> required_true{{
      "one_pass_schur", "fej_enabled", "global_3d_transient",
      "all_cameras_radtan", "camera_extrinsic_calibration_off",
      "camera_intrinsic_calibration_off",
      "camera_time_offset_calibration_off", "stereo_enabled",
      "stereo_available"}};
  for (const char *name : required_true)
    if (!bool_member(resolved, name, label + ".resolved_configuration"))
      throw ScanError(std::string("run header requires ") + name);
  if (!bool_member(resolved, "require_target_stereo_range",
                   label + ".resolved_configuration") ||
      uint64_member(resolved, "camera_count",
                    label + ".resolved_configuration") != 2U)
    throw ScanError("run header target-stereo/camera-count contract differs");

  const Json::Value &extensions = object_member(record, "extensions", label);
  const Json::Value &event =
      object_member(extensions, "event_extension", label + ".extensions");
  if (string_member(event, "schema", label + ".event_extension") !=
          "turnsafe.t0.event_extension.v1" ||
      uint64_member(event, "record_version",
                    label + ".event_extension") != 1U)
    throw ScanError("run-header event extension version differs");
  const Json::Value &flags = object_member(
      event, "capture_flags", label + ".event_extension");
  for (const char *name : {"causal_imu_intervals",
                           "outcome_association_keys",
                           "group_bearing_provenance"})
    if (!bool_member(flags, name,
                     label + ".event_extension.capture_flags"))
      throw ScanError(std::string("run header capture flag is false: ") + name);
  if (string_member(event, "canonical_ordering_version",
                    label + ".event_extension") !=
          "turnsafe.event_extension.order.v1" ||
      string_member(event, "image_cell_representation",
                    label + ".event_extension") !=
          "continuous_pixel_center_fraction.v1" ||
      string_member(event, "group_hash_algorithm",
                    label + ".event_extension") !=
          "fnv1a64_binary64_be.v1")
    throw ScanError("run-header event-extension conventions differ");

  HeaderIdentity output;
  output.frozen_base_sha = string_member(record, "frozen_base_sha", label);
  output.source_sha = string_member(record, "source_sha", label);
  output.tree_sha = string_member(record, "tree_sha", label);
  output.source_snapshot_sha256 =
      string_member(record, "source_snapshot_sha256", label);
  output.build_provenance_id =
      string_member(record, "build_provenance_id", label);
  output.config_sha256 = string_member(record, "config_sha256", label);
  output.calibration_sha256 =
      string_member(record, "calibration_sha256", label);
  return output;
}

std::map<TurnSafePairMemberKey, const Json::Value *, MemberKeyLess>
index_base_candidates(const Json::Value &candidates,
                      const std::string &label) {
  if (!candidates.isArray()) throw ScanError(label + " is not an array");
  std::map<TurnSafePairMemberKey, const Json::Value *, MemberKeyLess> output;
  TurnSafePairMemberKey previous;
  bool have_previous = false;
  MemberKeyLess less;
  for (Json::ArrayIndex index = 0U; index < candidates.size(); ++index) {
    const Json::Value &candidate = candidates[index];
    const std::string item_label = label + "[" + std::to_string(index) + "]";
    const TurnSafePairMemberKey key = parse_member_key(
        object_member(candidate, "candidate_key", item_label),
        item_label + ".candidate_key");
    if (have_previous && !less(previous, key))
      throw ScanError(label + " is not in strict canonical order");
    previous = key;
    have_previous = true;
    if (!output.emplace(key, &candidate).second)
      throw ScanError(label + " contains a duplicate candidate key");
  }
  return output;
}

using BaseGroupMembers =
    std::map<TurnSafePairGroupKey,
             std::set<TurnSafePairMemberKey, MemberKeyLess>, GroupKeyLess>;

BaseGroupMembers index_base_groups(
    const Json::Value &groups,
    const std::map<TurnSafePairMemberKey, const Json::Value *, MemberKeyLess>
        &base_candidates,
    const std::string &label) {
  if (!groups.isArray()) throw ScanError(label + " is not an array");
  BaseGroupMembers output;
  TurnSafePairGroupKey previous;
  bool have_previous = false;
  GroupKeyLess group_less;
  MemberKeyLess member_less;
  for (Json::ArrayIndex index = 0U; index < groups.size(); ++index) {
    const Json::Value &group = groups[index];
    const std::string group_label = label + "[" + std::to_string(index) + "]";
    const TurnSafePairGroupKey key = parse_group_key(
        object_member(group, "pair_key", group_label),
        group_label + ".pair_key");
    if (have_previous && !group_less(previous, key))
      throw ScanError(label + " is not in strict canonical order");
    previous = key;
    have_previous = true;
    const Json::Value &candidate_keys =
        array_member(group, "candidate_keys", group_label);
    if (uint64_member(group, "raw_candidate_count", group_label) !=
        candidate_keys.size())
      throw ScanError(group_label + " raw candidate count differs");
    std::set<TurnSafePairMemberKey, MemberKeyLess> members;
    TurnSafePairMemberKey previous_member;
    bool have_previous_member = false;
    for (Json::ArrayIndex member_index = 0U;
         member_index < candidate_keys.size(); ++member_index) {
      const TurnSafePairMemberKey member_key = parse_member_key(
          candidate_keys[member_index], group_label + ".candidate_keys[" +
                                            std::to_string(member_index) + "]");
      if (member_key.camera_id != key.camera_id ||
          member_key.source_timestamp_key != key.source_timestamp_key ||
          member_key.target_timestamp_key != key.target_timestamp_key)
        throw ScanError(group_label + " candidate key differs from group");
      if (have_previous_member && !member_less(previous_member, member_key))
        throw ScanError(group_label + " candidates are not canonical");
      previous_member = member_key;
      have_previous_member = true;
      if (base_candidates.find(member_key) == base_candidates.end() ||
          !members.insert(member_key).second)
        throw ScanError(group_label + " candidate is missing or duplicate");
    }
    if (!output.emplace(key, std::move(members)).second)
      throw ScanError(label + " contains duplicate pair key");
  }
  return output;
}

BaseGroupMembers expected_provenance_groups(
    const BaseGroupMembers &base_groups,
    const std::map<TurnSafePairMemberKey, const Json::Value *, MemberKeyLess>
        &base_candidates,
    const std::string &label) {
  BaseGroupMembers output;
  for (const auto &base_group : base_groups) {
    std::set<TurnSafePairMemberKey, MemberKeyLess> expected_members;
    for (const TurnSafePairMemberKey &key : base_group.second) {
      const auto candidate = base_candidates.find(key);
      if (candidate == base_candidates.end())
        throw ScanError(label + " base group references a missing candidate");
      const Json::Value &raw = *candidate->second;
      if (eligible_outcome(string_member(raw, "full_outcome", label)) &&
          bool_member(raw, "target_time_stereo_available", label)) {
        expected_members.insert(key);
      }
    }
    // Session-1E emits provenance only for typed, stereo-supported groups
    // with at least two distinct members. Bind the extension to that exact
    // filtered base population so no eligible member or whole group can be
    // silently omitted by the offline join.
    if (expected_members.size() >= 2U)
      output.emplace(base_group.first, std::move(expected_members));
  }
  return output;
}

struct CallbackMapping {
  TurnSafePilotCallbackInput input;
  std::string timestamp_key;
  std::string provenance_status;
  std::string provenance_reason;
  std::uint64_t provenance_members = 0U;
};

CallbackMapping map_callback(const Json::Value &record,
                             std::uint64_t expected_callback_index) {
  const std::string label = "callback[" +
                            std::to_string(expected_callback_index) + "]";
  if (string_member(record, "schema", label) != "turnsafe.t0.v1" ||
      string_member(record, "record_type", label) != "callback")
    throw ScanError(label + " has invalid base schema/type");
  CallbackMapping output;
  output.input.callback_index = uint64_member(record, "callback_index", label);
  if (output.input.callback_index != expected_callback_index)
    throw ScanError(label + " callback index is not contiguous");
  output.input.callback_timestamp =
      finite_number_member(record, "callback_timestamp_value", label);
  output.timestamp_key = string_member(record, "callback_timestamp_key", label);
  require_timestamp_key(output.timestamp_key, output.input.callback_timestamp,
                        label + ".callback_timestamp_key");

  const Json::Value &base_candidate_values =
      array_member(record, "shadow_candidates", label);
  const auto base_candidates =
      index_base_candidates(base_candidate_values, label + ".shadow_candidates");
  const Json::Value &base_group_values =
      array_member(record, "shadow_groups", label);
  const BaseGroupMembers base_groups = index_base_groups(
      base_group_values, base_candidates, label + ".shadow_groups");
  output.input.raw_group_count = base_groups.size();

  const Json::Value &extensions = object_member(record, "extensions", label);
  const Json::Value &event = object_member(
      extensions, "event_extension", label + ".extensions");
  if (string_member(event, "schema", label + ".event_extension") !=
          "turnsafe.t0.event_extension.v1" ||
      uint64_member(event, "record_version", label + ".event_extension") != 1U)
    throw ScanError(label + " event-extension version differs");
  const Json::Value &provenance = object_member(
      event, "group_bearing_provenance", label + ".event_extension");
  output.provenance_status =
      string_member(provenance, "status", label + ".provenance");
  output.provenance_reason =
      string_member(provenance, "reason", label + ".provenance");
  if (string_member(provenance, "schema_version", label + ".provenance") !=
          "turnsafe.t0.event_extension.v1" ||
      string_member(provenance, "canonical_ordering_version",
                    label + ".provenance") !=
          "turnsafe.event_extension.order.v1" ||
      string_member(provenance, "shared_field_encoding",
                    label + ".provenance") !=
          "one_group_record_plus_member_array.v1")
    throw ScanError(label + " provenance conventions differ");
  const Json::Value &groups = array_member(provenance, "groups",
                                           label + ".provenance");
  if (uint64_member(provenance, "group_count", label + ".provenance") !=
      groups.size())
    throw ScanError(label + " provenance group count differs");
  if ((output.provenance_status == "AVAILABLE") !=
          (output.provenance_reason == "NONE") ||
      (output.provenance_status != "AVAILABLE" && !groups.empty()))
    throw ScanError(label + " provenance status/reason/groups are inconsistent");
  const BaseGroupMembers expected_provenance = expected_provenance_groups(
      base_groups, base_candidates, label + ".expected_provenance");
  if (output.provenance_status == "AVAILABLE" &&
      groups.size() != expected_provenance.size())
    throw ScanError(label + " provenance omits or adds a typed stereo group");

  const PriorPrimitives prior = parse_prior_primitives(
      object_member(record, "prior_primitives", label), !groups.empty(),
      label + ".prior_primitives");
  for (const auto &camera : prior.cameras)
    output.input.cameras.push_back(camera.second.certificate);

  TurnSafePairGroupKey previous_group;
  bool have_previous_group = false;
  GroupKeyLess group_less;
  for (Json::ArrayIndex group_index = 0U; group_index < groups.size();
       ++group_index) {
    const Json::Value &raw_group = groups[group_index];
    const std::string group_label = label + ".provenance.groups[" +
                                    std::to_string(group_index) + "]";
    TurnSafePilotGroupInput group;
    group.key = parse_group_key(object_member(raw_group, "group_key", group_label),
                                group_label + ".group_key");
    if (have_previous_group && !group_less(previous_group, group.key))
      throw ScanError(label + " provenance groups are not canonical");
    previous_group = group.key;
    have_previous_group = true;
    const auto base_group = base_groups.find(group.key);
    if (base_group == base_groups.end())
      throw ScanError(group_label + " has no matching base shadow group");
    const auto expected_group = expected_provenance.find(group.key);
    if (expected_group == expected_provenance.end())
      throw ScanError(group_label + " is not an expected typed stereo group");
    if (string_member(raw_group, "bearing_provenance", group_label) !=
        "direct_native_normalized_unit_ray.v1")
      throw ScanError(group_label + " bearing provenance differs");
    group.source_timestamp = finite_number_member(
        raw_group, "source_clone_timestamp_value", group_label);
    group.target_timestamp = finite_number_member(
        raw_group, "target_clone_timestamp_value", group_label);
    require_timestamp_key(group.key.source_timestamp_key,
                          group.source_timestamp, group_label + ".source");
    require_timestamp_key(group.key.target_timestamp_key,
                          group.target_timestamp, group_label + ".target");

    const auto main_camera = prior.cameras.find(group.key.camera_id);
    if (main_camera == prior.cameras.end())
      throw ScanError(group_label + " main camera is absent from prior");
    audit_group_camera(object_member(raw_group, "camera", group_label),
                       main_camera->second, group_label + ".camera");
    const Json::Value &raw_stereo_camera =
        object_member(raw_group, "target_stereo_camera", group_label);
    require_status(raw_stereo_camera, group_label + ".target_stereo_camera",
                   "AVAILABLE", "NONE");
    const std::size_t stereo_camera_id = size_member(
        raw_stereo_camera, "camera_id", group_label + ".target_stereo_camera");
    if (stereo_camera_id == group.key.camera_id)
      throw ScanError(group_label + " stereo camera equals main camera");
    const auto stereo_camera = prior.cameras.find(stereo_camera_id);
    if (stereo_camera == prior.cameras.end())
      throw ScanError(group_label + " stereo camera is absent from prior");
    audit_group_camera(raw_stereo_camera, stereo_camera->second,
                       group_label + ".target_stereo_camera");

    const auto source_clone = prior.clones.find(group.key.source_timestamp_key);
    const auto target_clone = prior.clones.find(group.key.target_timestamp_key);
    if (source_clone == prior.clones.end() || target_clone == prior.clones.end() ||
        !same_double(source_clone->second.timestamp, group.source_timestamp) ||
        !same_double(target_clone->second.timestamp, group.target_timestamp))
      throw ScanError(group_label + " clone identity is absent or inconsistent");
    group.source_current_R_GtoI = eigen_matrix<3, 3>(parse_matrix(
        object_member(raw_group, "source_current_R_GtoI", group_label),
        3U, 3U, "values_row_major",
        group_label + ".source_current_R_GtoI"));
    group.source_fej_R_GtoI = eigen_matrix<3, 3>(parse_matrix(
        object_member(raw_group, "source_fej_R_GtoI", group_label),
        3U, 3U, "values_row_major",
        group_label + ".source_fej_R_GtoI"));
    group.target_current_R_GtoI = eigen_matrix<3, 3>(parse_matrix(
        object_member(raw_group, "target_current_R_GtoI", group_label),
        3U, 3U, "values_row_major",
        group_label + ".target_current_R_GtoI"));
    group.target_fej_R_GtoI = eigen_matrix<3, 3>(parse_matrix(
        object_member(raw_group, "target_fej_R_GtoI", group_label),
        3U, 3U, "values_row_major",
        group_label + ".target_fej_R_GtoI"));
    const Eigen::Matrix3d fixed_R_ItoC = eigen_matrix<3, 3>(parse_matrix(
        object_member(raw_group, "fixed_R_ItoC", group_label), 3U, 3U,
        "values_row_major", group_label + ".fixed_R_ItoC"));
    require_matrix_near(group.source_current_R_GtoI,
                        source_clone->second.current_R_GtoI,
                        group_label + ".source_current_R_GtoI");
    require_matrix_near(group.source_fej_R_GtoI,
                        source_clone->second.fej_R_GtoI,
                        group_label + ".source_fej_R_GtoI");
    require_matrix_near(group.target_current_R_GtoI,
                        target_clone->second.current_R_GtoI,
                        group_label + ".target_current_R_GtoI");
    require_matrix_near(group.target_fej_R_GtoI,
                        target_clone->second.fej_R_GtoI,
                        group_label + ".target_fej_R_GtoI");
    require_matrix_near(fixed_R_ItoC,
                        main_camera->second.certificate.R_ItoC,
                        group_label + ".fixed_R_ItoC");

    const Json::Value &supported = object_member(
        raw_group, "supported_configuration", group_label);
    if (!bool_member(supported, "value", group_label + ".supported_configuration") ||
        !array_member(supported, "reasons",
                      group_label + ".supported_configuration").empty())
      throw ScanError(group_label + " is outside supported configuration");

    const TimestampPairKey pair_key{group.key.source_timestamp_key,
                                    group.key.target_timestamp_key};
    const auto pair = prior.pairs.find(pair_key);
    if (pair == prior.pairs.end())
      throw ScanError(group_label + " pair covariance is absent from prior");
    TurnSafePilotPairPrior pair_prior;
    pair_prior.key = group.key;
    pair_prior.source_position_G = source_clone->second.current_position_G;
    pair_prior.target_position_G = target_clone->second.current_position_G;
    pair_prior.covariance = pair->second.covariance;
    output.input.pair_priors.push_back(pair_prior);

    const Json::Value &listed_members =
        array_member(raw_group, "member_key_list", group_label);
    const Json::Value &members = array_member(raw_group, "members", group_label);
    const std::size_t member_count = size_member(raw_group, "member_count", group_label);
    if (member_count < 2U || member_count != listed_members.size() ||
        member_count != members.size())
      throw ScanError(group_label + " member counts differ or are below two");
    TurnSafePairMemberKey previous_member;
    bool have_previous_member = false;
    MemberKeyLess member_less;
    std::set<TurnSafePairMemberKey, MemberKeyLess> listed_member_set;
    for (Json::ArrayIndex member_index = 0U; member_index < members.size();
         ++member_index) {
      const TurnSafePairMemberKey listed_key = parse_member_key(
          listed_members[member_index], group_label + ".member_key_list[" +
                                             std::to_string(member_index) + "]");
      if (have_previous_member && !member_less(previous_member, listed_key))
        throw ScanError(group_label + " members are not canonical");
      previous_member = listed_key;
      have_previous_member = true;
      if (base_group->second.count(listed_key) == 0U)
        throw ScanError(group_label + " member is absent from base group");
      if (!listed_member_set.insert(listed_key).second)
        throw ScanError(group_label + " contains a duplicate listed member");
      group.members.push_back(parse_group_member(
          members[member_index], listed_members[member_index], base_candidates,
          prior, group.key, main_camera->second, stereo_camera->second,
          group_label + ".members[" + std::to_string(member_index) + "]"));
    }
    if (listed_member_set.size() != expected_group->second.size() ||
        std::any_of(listed_member_set.begin(), listed_member_set.end(),
                    [&](const TurnSafePairMemberKey &key) {
                      return expected_group->second.count(key) == 0U;
                    }))
      throw ScanError(group_label +
                      " member list differs from exact typed stereo population");
    output.provenance_members += member_count;
    output.input.groups.push_back(std::move(group));
  }
  return output;
}

const std::vector<std::string> &csv_columns() {
  static const std::vector<std::string> columns = {
      "schema_version", "sequence", "run", "callback_index",
      "callback_timestamp_value", "callback_timestamp_key",
      "provenance_status", "provenance_reason", "pilot_status",
      "selection_status", "raw_groups", "typed_groups", "raw_members",
      "stereo_valid_members", "range_lcb_valid_members",
      "translation_ucb_valid_members", "acute_members",
      "rho_passed_members", "stereo_valid_groups",
      "range_lcb_valid_groups", "translation_ucb_valid_groups",
      "acute_groups", "rho_groups", "groups_n_ge_4", "consensus_groups",
      "spatial_groups", "rank_groups", "conditioning_groups",
      "winner_count", "winner_nis_evaluated_count",
      "winner_nis_pass_count", "winner_information_non_negligible_count",
      "foregone_eligible_groups",
      "foregone_eligible_features", "eligible_group_count",
      "certified_winner_available", "winner_camera_id",
      "winner_source_timestamp_key", "winner_target_timestamp_key",
      "winner_group_status", "winner_terminal_reason",
      "winner_selection_role", "winner_raw_member_count",
      "winner_retained_member_count",
      "winner_minimum_acute_margin", "winner_minimum_rho_margin",
      "winner_score_predicted_rotation_angle_rad",
      "winner_score_minimum_whitened_local_singular_value",
      "winner_score_maximum_retained_rho_trans",
      "winner_score_retained_feature_count",
      "winner_consensus_first_fit_rotation_angle_rad",
      "winner_consensus_median", "winner_consensus_mad",
      "winner_consensus_robust_threshold",
      "winner_consensus_required_retained_count",
      "winner_consensus_refit_rotation_angle_rad",
      "winner_spatial_convex_hull_area",
      "winner_spatial_minimum_population_covariance_eigenvalue",
      "winner_spatial_occupied_fixed_4x4_cells", "winner_spatial_x_span",
      "winner_spatial_y_span", "winner_rotation_stack_sigma_1",
      "winner_rotation_stack_sigma_2", "winner_rotation_stack_sigma_3",
      "winner_rotation_stack_rank_tolerance", "winner_rotation_stack_rank",
      "winner_rotation_stack_condition_ratio",
      "winner_whitened_local_stack_sigma_1",
      "winner_whitened_local_stack_sigma_2",
      "winner_whitened_local_stack_sigma_3", "winner_factor_status",
      "winner_predicted_information_trace",
      "winner_predicted_information_determinant",
      "winner_predicted_information_eigenvalue_min",
      "winner_predicted_information_eigenvalue_middle",
      "winner_predicted_information_eigenvalue_max",
      "winner_predicted_information_non_negligible",
      "winner_nis", "winner_nis_degrees_of_freedom",
      "winner_nis_threshold", "winner_nis_passed",
      "winner_information_trace", "winner_information_determinant",
      "winner_information_eigenvalue_min",
      "winner_information_eigenvalue_middle",
      "winner_information_eigenvalue_max", "winner_relative_stack_sigma_1",
      "winner_relative_stack_sigma_2", "winner_relative_stack_sigma_3",
      "winner_information_non_negligible",
      "post_nis_frozen_winner_available",
      "post_nis_accepted_winner_available", "post_nis_runner_up_promoted",
      "rejection_reasons_json", "group_diagnostics_json",
      "candidate_diagnostics_json", "winner_candidate_diagnostics_json"};
  return columns;
}

void write_csv_header(std::ostream &output) {
  const auto &columns = csv_columns();
  for (std::size_t index = 0U; index < columns.size(); ++index) {
    if (index != 0U) output << ',';
    output << columns[index];
  }
  output << '\n';
  if (!output) throw ScanError("failed to write callback CSV header");
}

Json::Value member_key_json(const TurnSafePairMemberKey &key) {
  Json::Value output(Json::objectValue);
  output["camera_id"] = static_cast<Json::UInt64>(key.camera_id);
  output["detached_index"] = static_cast<Json::UInt64>(key.detached_index);
  output["feature_id"] = static_cast<Json::UInt64>(key.feature_id);
  output["source_observation_ordinal"] =
      static_cast<Json::UInt64>(key.source_observation_ordinal);
  output["source_timestamp_key"] = key.source_timestamp_key;
  output["target_observation_ordinal"] =
      static_cast<Json::UInt64>(key.target_observation_ordinal);
  output["target_timestamp_key"] = key.target_timestamp_key;
  return output;
}

void json_number(Json::Value &object, const char *name, double value) {
  if (std::isfinite(value)) object[name] = value;
  else object[name] = Json::Value(Json::nullValue);
}

Json::Value group_diagnostics_json(const TurnSafePilotCallbackResult &result) {
  Json::Value output(Json::arrayValue);
  for (const TurnSafePairGroupResult &group : result.selection.groups) {
    Json::Value item(Json::objectValue);
    item["camera_id"] = static_cast<Json::UInt64>(group.key.camera_id);
    item["source_timestamp_key"] = group.key.source_timestamp_key;
    item["target_timestamp_key"] = group.key.target_timestamp_key;
    item["status"] = ov_msckf::turnsafe_pair_group_status_name(group.status);
    item["terminal_reason"] =
        ov_msckf::turnsafe_pair_terminal_reason_name(group.terminal_reason);
    item["selection_role"] =
        ov_msckf::turnsafe_pair_selection_role_name(group.selection_role);
    item["raw_member_count"] =
        static_cast<Json::UInt64>(group.raw_member_count);
    item["retained_member_count"] =
        static_cast<Json::UInt64>(group.consensus.retained_member_keys.size());
    item["consensus_attempted"] = group.consensus.attempted;
    item["consensus_first_fit_available"] =
        group.consensus.first_fit_available;
    item["consensus_refit_available"] = group.consensus.refit_available;
    Json::Value retained_keys(Json::arrayValue);
    for (const TurnSafePairMemberKey &key :
         group.consensus.retained_member_keys)
      retained_keys.append(member_key_json(key));
    item["retained_member_keys"] = std::move(retained_keys);
    Json::Value consensus_members(Json::arrayValue);
    for (const auto &member_diagnostic : group.consensus.members) {
      Json::Value member(Json::objectValue);
      member["key"] = member_key_json(member_diagnostic.key);
      member["retained"] = member_diagnostic.retained;
      json_number(member, "initial_whitened_norm",
                  member_diagnostic.initial_whitened_norm);
      member["refit_norm_available"] =
          member_diagnostic.refit_norm_available;
      json_number(member, "refit_whitened_norm",
                  member_diagnostic.refit_whitened_norm);
      consensus_members.append(std::move(member));
    }
    item["consensus_members"] = std::move(consensus_members);
    json_number(item, "predicted_rotation_angle_rad",
                group.score.predicted_rotation_angle_rad);
    json_number(item, "minimum_whitened_local_singular_value",
                group.score.minimum_whitened_local_singular_value);
    json_number(item, "maximum_retained_rho_trans",
                group.score.maximum_retained_rho_trans);
    item["retained_feature_count"] =
        static_cast<Json::UInt64>(group.score.retained_feature_count);
    json_number(item, "convex_hull_area", group.spatial.convex_hull_area);
    json_number(item, "minimum_population_covariance_eigenvalue",
                group.spatial.minimum_population_covariance_eigenvalue);
    item["occupied_fixed_4x4_cells"] =
        static_cast<Json::UInt64>(group.spatial.occupied_fixed_4x4_cells);
    json_number(item, "x_span", group.spatial.x_span);
    json_number(item, "y_span", group.spatial.y_span);
    item["spatial_attempted"] = group.spatial.attempted;
    const bool spatial_passed = group.spatial.attempted &&
        std::isfinite(group.spatial.convex_hull_area) &&
        std::isfinite(
            group.spatial.minimum_population_covariance_eigenvalue) &&
        std::isfinite(group.spatial.x_span) &&
        std::isfinite(group.spatial.y_span) &&
        group.spatial.convex_hull_area >= 0.1 &&
        group.spatial.minimum_population_covariance_eigenvalue >= 0.03 &&
        group.spatial.occupied_fixed_4x4_cells >= 4U &&
        group.spatial.x_span >= 0.6 && group.spatial.y_span >= 0.6;
    item["spatial_passed"] = spatial_passed;
    item["rotation_stack_rank"] =
        static_cast<Json::UInt64>(group.rotation_stack_rank);
    json_number(item, "rotation_stack_rank_tolerance",
                group.rotation_stack_rank_tolerance);
    json_number(item, "rotation_stack_condition_ratio",
                group.rotation_stack_condition_ratio);
    item["rank_passed"] = group.rotation_stack_rank == 3U;
    item["conditioning_passed"] = group.rotation_stack_rank == 3U &&
        std::isfinite(group.rotation_stack_condition_ratio) &&
        group.rotation_stack_condition_ratio >= 0.15;
    Json::Value rotation_singular_values(Json::arrayValue);
    Json::Value whitened_singular_values(Json::arrayValue);
    for (int index = 0; index < 3; ++index) {
      rotation_singular_values.append(
          group.rotation_stack_singular_values(index));
      whitened_singular_values.append(
          group.whitened_local_stack_singular_values(index));
    }
    item["rotation_stack_singular_values_descending"] =
        std::move(rotation_singular_values);
    item["whitened_local_stack_singular_values_descending"] =
        std::move(whitened_singular_values);
    if (group.status == ov_msckf::TurnSafePairGroupStatus::kEligible) {
      json_number(item, "predicted_orientation_information_trace",
                  group.predicted_orientation_information_trace);
      json_number(item, "predicted_orientation_information_determinant",
                  group.predicted_orientation_information_determinant);
      Json::Value information_eigenvalues(Json::arrayValue);
      for (int index = 0; index < 3; ++index)
        information_eigenvalues.append(
            group.predicted_orientation_information_eigenvalues(index));
      item["predicted_orientation_information_eigenvalues_ascending"] =
          std::move(information_eigenvalues);
      item["predicted_orientation_information_non_negligible"] =
          group.predicted_orientation_information_non_negligible;
    }
    output.append(std::move(item));
  }
  return output;
}

Json::Value candidate_diagnostic_json(
    const ov_msckf::TurnSafePilotGroupEvaluation &evaluation,
    const ov_msckf::TurnSafePilotCandidateResult &candidate) {
  Json::Value item(Json::objectValue);
  item["group_camera_id"] =
      static_cast<Json::UInt64>(evaluation.key.camera_id);
  item["group_source_timestamp_key"] =
      evaluation.key.source_timestamp_key;
  item["group_target_timestamp_key"] =
      evaluation.key.target_timestamp_key;
  item["key"] = member_key_json(candidate.key);
  item["stereo_valid"] = candidate.stereo_valid;
  item["range_lcb_valid"] = candidate.range_lcb_valid;
  item["translation_ucb_valid"] = candidate.translation_ucb_valid;
  item["acute"] = candidate.acute;
  item["rho_passed"] = candidate.rho_passed;
  item["terminal_reason"] = candidate.terminal_reason;
  item["source_bearing_status"] =
      ov_msckf::turnsafe_certificate_status_name(candidate.source_bearing.status);
  item["target_bearing_status"] =
      ov_msckf::turnsafe_certificate_status_name(candidate.target_bearing.status);

  Json::Value range(Json::objectValue);
  range["status"] =
      ov_msckf::turnsafe_certificate_status_name(candidate.stereo_range.status);
  json_number(range, "d_hat", candidate.stereo_range.d_hat);
  json_number(range, "range_variance", candidate.stereo_range.range_variance);
  json_number(range, "range_standard_deviation",
              candidate.stereo_range.range_standard_deviation);
  json_number(range, "normal_quantile",
              candidate.stereo_range.normal_quantile);
  json_number(range, "d_lcb", candidate.stereo_range.d_lcb);
  item["range"] = std::move(range);

  Json::Value translation(Json::objectValue);
  translation["status"] = ov_msckf::turnsafe_certificate_status_name(
      candidate.translation.status);
  json_number(translation, "covariance_inflation",
              candidate.translation.covariance_inflation);
  json_number(translation, "chi_square_quantile",
              candidate.translation.chi_square_quantile);
  json_number(translation, "chi_square_radius",
              candidate.translation.chi_square_radius);
  json_number(translation, "certified_lambda_max",
              candidate.translation.certified_lambda_max);
  json_number(translation, "t_ucb", candidate.translation.t_ucb);
  item["translation"] = std::move(translation);

  Json::Value factor(Json::objectValue);
  factor["status"] =
      ov_msckf::turnsafe_bearing_factor_status_name(candidate.factor.status);
  if (candidate.factor.accepted() && candidate.factor.residual.allFinite()) {
    Json::Value residual(Json::arrayValue);
    residual.append(candidate.factor.residual(0));
    residual.append(candidate.factor.residual(1));
    factor["residual"] = std::move(residual);
  }
  if (candidate.factor.accepted() && candidate.factor.Sigma_R.allFinite()) {
    Json::Value covariance(Json::arrayValue);
    for (int row = 0; row < 2; ++row)
      for (int col = 0; col < 2; ++col)
        covariance.append(candidate.factor.Sigma_R(row, col));
    factor["Sigma_R_row_major"] = std::move(covariance);
  }
  item["factor"] = std::move(factor);

  Json::Value certificate(Json::objectValue);
  certificate["status"] = ov_msckf::turnsafe_certificate_status_name(
      candidate.certificate.status);
  certificate["acute"] = candidate.certificate.acute;
  certificate["theta_available"] = candidate.certificate.theta_available;
  certificate["rho_available"] = candidate.certificate.rho_available;
  json_number(certificate, "theta_trans_ucb",
              candidate.certificate.theta_trans_ucb);
  json_number(certificate, "lambda_min", candidate.certificate.lambda_min);
  json_number(certificate, "lambda_max", candidate.certificate.lambda_max);
  json_number(certificate, "reciprocal_condition",
              candidate.certificate.reciprocal_condition);
  json_number(certificate, "sigma_bearing_worst",
              candidate.certificate.sigma_bearing_worst);
  json_number(certificate, "rho_trans", candidate.certificate.rho_trans);
  if (std::isfinite(candidate.stereo_range.d_lcb) &&
      std::isfinite(candidate.translation.t_ucb))
    certificate["acute_margin_d_lcb_minus_t_ucb"] =
        candidate.stereo_range.d_lcb - candidate.translation.t_ucb;
  if (std::isfinite(candidate.certificate.rho_trans))
    certificate["rho_margin_to_0_5"] =
        ov_msckf::TurnSafeCertificate::kRhoMaximum -
        candidate.certificate.rho_trans;
  item["certificate"] = std::move(certificate);
  return item;
}

Json::Value candidate_diagnostics_json(
    const TurnSafePilotCallbackResult &result) {
  Json::Value output(Json::arrayValue);
  for (const auto &evaluation : result.evaluations) {
    for (const auto &candidate : evaluation.candidates)
      output.append(candidate_diagnostic_json(evaluation, candidate));
  }
  return output;
}

Json::Value winner_candidate_diagnostics_json(
    const TurnSafePilotCallbackResult &result) {
  Json::Value output(Json::arrayValue);
  if (!result.selection.winner_available) return output;
  const TurnSafePairGroupKey &winner = result.selection.winner_key;
  for (const auto &evaluation : result.evaluations) {
    if (!ov_msckf::turnsafe_pair_group_key_equal(evaluation.key, winner))
      continue;
    for (const auto &candidate : evaluation.candidates)
      output.append(candidate_diagnostic_json(evaluation, candidate));
    break;
  }
  return output;
}

void write_callback_row(std::ostream &stream, const Options &options,
                        const CallbackMapping &mapping,
                        const TurnSafePilotCallbackResult &result,
                        Aggregate &aggregate) {
  std::map<std::string, std::string> row;
  row["schema_version"] = "turnsafe.t1.p1.shadow_callback.v1";
  row["sequence"] = options.sequence;
  row["run"] = options.run;
  row["callback_index"] = std::to_string(result.callback_index);
  row["callback_timestamp_value"] = numeric(result.callback_timestamp);
  row["callback_timestamp_key"] = mapping.timestamp_key;
  row["provenance_status"] = mapping.provenance_status;
  row["provenance_reason"] = mapping.provenance_reason;
  row["pilot_status"] =
      ov_msckf::turnsafe_pilot_shadow_status_name(result.status);
  row["selection_status"] =
      ov_msckf::turnsafe_pair_selection_status_name(result.selection.status);
  const auto &c = result.counters;
  if (c.winner_count > 1U || c.winner_nis_evaluated_count > 1U ||
      c.winner_nis_pass_count > 1U ||
      c.winner_information_non_negligible_count > 1U ||
      c.winner_count != static_cast<std::size_t>(
          result.selection.winner_available))
    throw ScanError("PilotShadow winner lifecycle counters are invalid");
  const bool winner_nis_values_complete =
      result.winner_factor.degrees_of_freedom > 0 &&
      std::isfinite(result.winner_factor.nis) &&
      result.winner_factor.nis >= 0.0 &&
      std::isfinite(result.winner_factor.nis_threshold) &&
      result.winner_factor.nis_threshold > 0.0;
  const bool winner_nis_evaluated =
      c.winner_nis_evaluated_count == 1U;
  if (winner_nis_evaluated !=
          (result.selection.winner_available &&
           winner_nis_values_complete) ||
      c.winner_nis_pass_count != static_cast<std::size_t>(
          winner_nis_evaluated && result.winner_factor.nis_passed) ||
      c.winner_information_non_negligible_count !=
          static_cast<std::size_t>(
              result.winner_factor.accepted() &&
              result.winner_factor.information_non_negligible))
    throw ScanError("PilotShadow winner factor lifecycle is inconsistent");
  const bool expected_post_nis_accepted =
      result.selection.winner_available && winner_nis_evaluated &&
      result.winner_factor.accepted() &&
      result.winner_factor.nis_passed;
  if (result.post_nis.frozen_winner_available !=
          result.selection.winner_available ||
      result.post_nis.winner_nis_passed != expected_post_nis_accepted ||
      result.post_nis.accepted_winner_available !=
          expected_post_nis_accepted ||
      result.post_nis.runner_up_promoted)
    throw ScanError("PilotShadow post-NIS lifecycle is inconsistent");
  const std::array<std::pair<const char *, std::size_t>, 24U> counters{{
      {"raw_groups", c.raw_groups}, {"typed_groups", c.typed_groups},
      {"raw_members", c.raw_members},
      {"stereo_valid_members", c.stereo_valid_members},
      {"range_lcb_valid_members", c.range_lcb_valid_members},
      {"translation_ucb_valid_members", c.translation_ucb_valid_members},
      {"acute_members", c.acute_members},
      {"rho_passed_members", c.rho_passed_members},
      {"stereo_valid_groups", c.stereo_valid_groups},
      {"range_lcb_valid_groups", c.range_lcb_valid_groups},
      {"translation_ucb_valid_groups", c.translation_ucb_valid_groups},
      {"acute_groups", c.acute_groups}, {"rho_groups", c.rho_groups},
      {"groups_n_ge_4", c.groups_n_ge_4},
      {"consensus_groups", c.consensus_groups},
      {"spatial_groups", c.spatial_groups}, {"rank_groups", c.rank_groups},
      {"conditioning_groups", c.conditioning_groups},
      {"winner_count", c.winner_count},
      {"winner_nis_evaluated_count", c.winner_nis_evaluated_count},
      {"winner_nis_pass_count", c.winner_nis_pass_count},
      {"winner_information_non_negligible_count",
       c.winner_information_non_negligible_count},
      {"foregone_eligible_groups", c.foregone_eligible_groups},
      {"foregone_eligible_features", c.foregone_eligible_features}}};
  for (const auto &entry : counters) row[entry.first] = std::to_string(entry.second);
  std::size_t eligible_group_count = 0U;
  for (const TurnSafePairGroupResult &group : result.selection.groups)
    if (group.status == ov_msckf::TurnSafePairGroupStatus::kEligible)
      ++eligible_group_count;
  row["eligible_group_count"] = std::to_string(eligible_group_count);
  row["certified_winner_available"] =
      boolean(result.selection.winner_available);
  row["post_nis_frozen_winner_available"] =
      boolean(result.post_nis.frozen_winner_available);
  row["post_nis_accepted_winner_available"] =
      boolean(result.post_nis.accepted_winner_available);
  row["post_nis_runner_up_promoted"] =
      boolean(result.post_nis.runner_up_promoted);

  if (result.selection.winner_available) {
    const TurnSafePairGroupResult &winner =
        result.selection.groups.at(result.selection.winner_index);
    row["winner_camera_id"] = std::to_string(winner.key.camera_id);
    row["winner_source_timestamp_key"] = winner.key.source_timestamp_key;
    row["winner_target_timestamp_key"] = winner.key.target_timestamp_key;
    row["winner_group_status"] =
        ov_msckf::turnsafe_pair_group_status_name(winner.status);
    row["winner_terminal_reason"] =
        ov_msckf::turnsafe_pair_terminal_reason_name(winner.terminal_reason);
    row["winner_selection_role"] =
        ov_msckf::turnsafe_pair_selection_role_name(winner.selection_role);
    row["winner_raw_member_count"] = std::to_string(winner.raw_member_count);
    row["winner_retained_member_count"] =
        std::to_string(winner.consensus.retained_member_keys.size());
    double minimum_acute_margin = std::numeric_limits<double>::infinity();
    double minimum_rho_margin = std::numeric_limits<double>::infinity();
    std::size_t retained_margin_count = 0U;
    for (const auto &evaluation : result.evaluations) {
      if (!ov_msckf::turnsafe_pair_group_key_equal(evaluation.key,
                                                    winner.key))
        continue;
      for (const TurnSafePairMemberKey &retained_key :
           winner.consensus.retained_member_keys) {
        const auto retained = std::find_if(
            evaluation.candidates.begin(), evaluation.candidates.end(),
            [&](const ov_msckf::TurnSafePilotCandidateResult &candidate) {
              return ov_msckf::turnsafe_pair_member_key_equal(candidate.key,
                                                               retained_key);
            });
        if (retained == evaluation.candidates.end() ||
            !std::isfinite(retained->stereo_range.d_lcb) ||
            !std::isfinite(retained->translation.t_ucb) ||
            !std::isfinite(retained->certificate.rho_trans))
          throw ScanError("retained winner lacks finite certificate margin");
        minimum_acute_margin = std::min(
            minimum_acute_margin,
            retained->stereo_range.d_lcb - retained->translation.t_ucb);
        minimum_rho_margin = std::min(
            minimum_rho_margin,
            ov_msckf::TurnSafeCertificate::kRhoMaximum -
                retained->certificate.rho_trans);
        ++retained_margin_count;
      }
      break;
    }
    if (retained_margin_count != winner.consensus.retained_member_keys.size() ||
        retained_margin_count == 0U || !std::isfinite(minimum_acute_margin) ||
        !std::isfinite(minimum_rho_margin))
      throw ScanError("retained winner certificate margins are incomplete");
    row["winner_minimum_acute_margin"] = numeric(minimum_acute_margin);
    row["winner_minimum_rho_margin"] = numeric(minimum_rho_margin);
    row["winner_score_predicted_rotation_angle_rad"] =
        numeric(winner.score.predicted_rotation_angle_rad);
    row["winner_score_minimum_whitened_local_singular_value"] =
        numeric(winner.score.minimum_whitened_local_singular_value);
    row["winner_score_maximum_retained_rho_trans"] =
        numeric(winner.score.maximum_retained_rho_trans);
    row["winner_score_retained_feature_count"] =
        std::to_string(winner.score.retained_feature_count);
    row["winner_consensus_first_fit_rotation_angle_rad"] =
        numeric(winner.consensus.first_fit_rotation_angle_rad);
    row["winner_consensus_median"] = numeric(winner.consensus.median);
    row["winner_consensus_mad"] = numeric(winner.consensus.mad);
    row["winner_consensus_robust_threshold"] =
        numeric(winner.consensus.robust_threshold);
    row["winner_consensus_required_retained_count"] =
        std::to_string(winner.consensus.required_retained_count);
    row["winner_consensus_refit_rotation_angle_rad"] =
        numeric(winner.consensus.refit_rotation_angle_rad);
    row["winner_spatial_convex_hull_area"] =
        numeric(winner.spatial.convex_hull_area);
    row["winner_spatial_minimum_population_covariance_eigenvalue"] =
        numeric(winner.spatial.minimum_population_covariance_eigenvalue);
    row["winner_spatial_occupied_fixed_4x4_cells"] =
        std::to_string(winner.spatial.occupied_fixed_4x4_cells);
    row["winner_spatial_x_span"] = numeric(winner.spatial.x_span);
    row["winner_spatial_y_span"] = numeric(winner.spatial.y_span);
    for (int index = 0; index < 3; ++index) {
      row["winner_rotation_stack_sigma_" + std::to_string(index + 1)] =
          numeric(winner.rotation_stack_singular_values(index));
      row["winner_whitened_local_stack_sigma_" +
          std::to_string(index + 1)] =
          numeric(winner.whitened_local_stack_singular_values(index));
    }
    row["winner_rotation_stack_rank_tolerance"] =
        numeric(winner.rotation_stack_rank_tolerance);
    row["winner_rotation_stack_rank"] =
        std::to_string(winner.rotation_stack_rank);
    row["winner_rotation_stack_condition_ratio"] =
        numeric(winner.rotation_stack_condition_ratio);
    row["winner_factor_status"] =
        ov_msckf::turnsafe_bearing_group_status_name(result.winner_factor.status);
    row["winner_predicted_information_trace"] =
        numeric(winner.predicted_orientation_information_trace);
    row["winner_predicted_information_determinant"] =
        numeric(winner.predicted_orientation_information_determinant);
    row["winner_predicted_information_eigenvalue_min"] =
        numeric(winner.predicted_orientation_information_eigenvalues(0));
    row["winner_predicted_information_eigenvalue_middle"] =
        numeric(winner.predicted_orientation_information_eigenvalues(1));
    row["winner_predicted_information_eigenvalue_max"] =
        numeric(winner.predicted_orientation_information_eigenvalues(2));
    row["winner_predicted_information_non_negligible"] = boolean(
        winner.predicted_orientation_information_non_negligible);
    if (winner_nis_evaluated) {
      row["winner_nis"] = numeric(result.winner_factor.nis);
      row["winner_nis_degrees_of_freedom"] =
          std::to_string(result.winner_factor.degrees_of_freedom);
      row["winner_nis_threshold"] =
          numeric(result.winner_factor.nis_threshold);
      row["winner_nis_passed"] = boolean(result.winner_factor.nis_passed);
    }
    if (result.winner_factor.accepted()) {
      row["winner_information_trace"] =
          numeric(result.winner_factor.information_trace);
      row["winner_information_determinant"] =
          numeric(result.winner_factor.information_determinant);
      row["winner_information_eigenvalue_min"] =
          numeric(result.winner_factor.information_eigenvalues(0));
      row["winner_information_eigenvalue_middle"] =
          numeric(result.winner_factor.information_eigenvalues(1));
      row["winner_information_eigenvalue_max"] =
          numeric(result.winner_factor.information_eigenvalues(2));
      for (int index = 0; index < 3; ++index)
        row["winner_relative_stack_sigma_" + std::to_string(index + 1)] =
            numeric(result.winner_factor.relative_stack_singular_values(index));
      row["winner_information_non_negligible"] =
          boolean(result.winner_factor.information_non_negligible);
    }
  }

  Json::Value reasons(Json::objectValue);
  for (const auto &reason : c.rejection_reasons) {
    reasons[reason.first] = static_cast<Json::UInt64>(reason.second);
    aggregate.rejection_reasons[reason.first] += reason.second;
  }
  row["rejection_reasons_json"] = compact_json(reasons);
  row["group_diagnostics_json"] =
      compact_json(group_diagnostics_json(result));
  row["candidate_diagnostics_json"] =
      compact_json(candidate_diagnostics_json(result));
  row["winner_candidate_diagnostics_json"] =
      compact_json(winner_candidate_diagnostics_json(result));
  for (std::size_t index = 0U; index < csv_columns().size(); ++index) {
    if (index != 0U) stream << ',';
    stream << csv_escape(row[csv_columns()[index]]);
  }
  stream << '\n';
  if (!stream) throw ScanError("failed to write callback CSV row");

  ++aggregate.callback_rows;
  if (result.selection.winner_available) ++aggregate.winner_callbacks;
  if (c.winner_nis_pass_count == 1U)
    ++aggregate.nis_passing_callbacks;
  if (result.post_nis.accepted_winner_available)
    ++aggregate.accepted_winner_callbacks;
  if (mapping.provenance_status == "AVAILABLE")
    ++aggregate.provenance_available_callbacks;
  aggregate.provenance_group_count += mapping.input.groups.size();
  aggregate.provenance_member_count += mapping.provenance_members;
  aggregate.winner_nis_evaluated_count += c.winner_nis_evaluated_count;
  aggregate.winner_nis_pass_count += c.winner_nis_pass_count;
  aggregate.winner_information_non_negligible_count +=
      c.winner_information_non_negligible_count;
}

Json::Value identity_json(const HeaderIdentity &identity) {
  Json::Value output(Json::objectValue);
  output["build_provenance_id"] = identity.build_provenance_id;
  output["calibration_sha256"] = identity.calibration_sha256;
  output["config_sha256"] = identity.config_sha256;
  output["frozen_base_sha"] = identity.frozen_base_sha;
  output["source_sha"] = identity.source_sha;
  output["source_snapshot_sha256"] = identity.source_snapshot_sha256;
  output["tree_sha"] = identity.tree_sha;
  return output;
}

void write_metadata(const std::string &path, const Options &options,
                    const FileIdentity &archive_identity,
                    const FileIdentity &raw_identity,
                    std::uint64_t record_count,
                    const FileIdentity &csv_identity,
                    const HeaderIdentity &header_identity,
                    const Aggregate &aggregate) {
  Json::Value root(Json::objectValue);
  root["schema_version"] = "turnsafe.t1.p1.shadow_scan_metadata.v1";
  root["status"] = "COMPLETE";
  root["sequence"] = options.sequence;
  root["run"] = options.run;

  Json::Value archive(Json::objectValue);
  archive["path"] = options.archive;
  archive["expected_sha256"] = options.expected_archive_sha256;
  archive["actual_sha256"] = archive_identity.sha256;
  archive["expected_size_bytes"] =
      static_cast<Json::UInt64>(options.expected_archive_size);
  archive["actual_size_bytes"] =
      static_cast<Json::UInt64>(archive_identity.size);
  archive["verified"] = true;
  root["archive_identity"] = std::move(archive);

  Json::Value raw(Json::objectValue);
  raw["expected_sha256"] = options.expected_raw_sha256;
  raw["actual_sha256"] = raw_identity.sha256;
  raw["expected_size_bytes"] =
      static_cast<Json::UInt64>(options.expected_raw_size);
  raw["actual_size_bytes"] = static_cast<Json::UInt64>(raw_identity.size);
  raw["verified"] = true;
  root["raw_identity"] = std::move(raw);

  Json::Value records(Json::objectValue);
  records["expected_record_count"] =
      static_cast<Json::UInt64>(options.expected_record_count);
  records["actual_record_count"] = static_cast<Json::UInt64>(record_count);
  records["callback_count"] =
      static_cast<Json::UInt64>(aggregate.callback_rows);
  records["run_header_count"] = static_cast<Json::UInt64>(1U);
  root["records"] = std::move(records);

  Json::Value csv(Json::objectValue);
  csv["path"] = options.callback_csv;
  csv["sha256"] = csv_identity.sha256;
  csv["size_bytes"] = static_cast<Json::UInt64>(csv_identity.size);
  csv["row_count"] = static_cast<Json::UInt64>(aggregate.callback_rows);
  csv["column_count"] =
      static_cast<Json::UInt64>(csv_columns().size());
  csv["row_schema_version"] = "turnsafe.t1.p1.shadow_callback.v1";
  root["callback_csv"] = std::move(csv);
  root["run_header_identity"] = identity_json(header_identity);

  Json::Value validation(Json::objectValue);
  validation["archive_streamed_with_libzstd"] = true;
  validation["archive_sha256_openssl"] = true;
  validation["raw_sha256_openssl"] = true;
  validation["duplicate_json_keys_rejected"] = true;
  validation["recursive_nonfinite_rejected"] = true;
  validation["schema_and_identity_audited"] = true;
  validation["jpl_xyzw_repository_rotation_audited"] = true;
  validation["calibration_hashes_audited"] = true;
  validation["clone_cross_covariance_blocks_audited"] = true;
  validation["maximum_jsonl_record_bytes"] =
      static_cast<Json::UInt64>(kMaximumJsonlRecordBytes);
  validation["newline_terminated_jsonl"] = true;
  validation["one_row_per_callback"] = true;
  root["validation"] = std::move(validation);

  Json::Value totals(Json::objectValue);
  totals["accepted_winner_callbacks"] =
      static_cast<Json::UInt64>(aggregate.accepted_winner_callbacks);
  totals["certified_winner_callbacks"] =
      static_cast<Json::UInt64>(aggregate.winner_callbacks);
  totals["nis_passing_callbacks"] =
      static_cast<Json::UInt64>(aggregate.nis_passing_callbacks);
  totals["winner_nis_evaluated_count"] =
      static_cast<Json::UInt64>(aggregate.winner_nis_evaluated_count);
  totals["winner_nis_pass_count"] =
      static_cast<Json::UInt64>(aggregate.winner_nis_pass_count);
  totals["winner_information_non_negligible_count"] =
      static_cast<Json::UInt64>(
          aggregate.winner_information_non_negligible_count);
  totals["provenance_available_callbacks"] =
      static_cast<Json::UInt64>(aggregate.provenance_available_callbacks);
  totals["provenance_group_count"] =
      static_cast<Json::UInt64>(aggregate.provenance_group_count);
  totals["provenance_member_count"] =
      static_cast<Json::UInt64>(aggregate.provenance_member_count);
  Json::Value reasons(Json::objectValue);
  for (const auto &reason : aggregate.rejection_reasons)
    reasons[reason.first] = static_cast<Json::UInt64>(reason.second);
  totals["rejection_reasons"] = std::move(reasons);
  root["totals"] = std::move(totals);

  Json::Value hard_stop(Json::objectValue);
  hard_stop["live_estimator_types_absent"] = true;
  hard_stop["live_proposal_calls_absent"] = true;
  hard_stop["state_mutation_absent"] = true;
  hard_stop["t1_rows_emitted_to_estimator"] = static_cast<Json::UInt64>(0U);
  root["hard_stop"] = std::move(hard_stop);

  Json::StreamWriterBuilder builder;
  builder["commentStyle"] = "None";
  builder["emitUTF8"] = true;
  builder["indentation"] = "  ";
  builder["precision"] = 17;
  builder["precisionType"] = "significant";
  std::ofstream output(path, std::ios::binary | std::ios::trunc);
  if (!output) throw ScanError("cannot create metadata temporary file");
  output << Json::writeString(builder, root) << '\n';
  output.flush();
  if (!output) throw ScanError("failed to write metadata temporary file");
  output.close();
  if (!output) throw ScanError("failed to close metadata temporary file");
}

struct StreamIdentities {
  FileIdentity archive;
  FileIdentity raw;
  std::uint64_t records = 0U;
  HeaderIdentity header;
};

StreamIdentities scan_archive(const Options &options, std::ostream &csv,
                              Aggregate &aggregate) {
  std::ifstream archive(options.archive, std::ios::binary);
  if (!archive) throw ScanError("failed to open telemetry archive");
  std::unique_ptr<ZSTD_DStream, decltype(&ZSTD_freeDStream)> decoder(
      ZSTD_createDStream(), &ZSTD_freeDStream);
  if (!decoder) throw ScanError("libzstd stream allocation failed");
  const std::size_t initialized = ZSTD_initDStream(decoder.get());
  if (ZSTD_isError(initialized))
    throw ScanError("libzstd stream initialization failed");
  const std::size_t output_capacity = ZSTD_DStreamOutSize();
  if (output_capacity == 0U)
    throw ScanError("libzstd reported zero output buffer size");
  std::vector<char> decompressed(output_capacity);
  std::array<char, 1024U * 1024U> compressed{};
  std::string line_buffer;
  line_buffer.reserve(1024U * 1024U);
  Sha256 archive_digest;
  Sha256 raw_digest;
  StreamIdentities identities;
  bool frame_complete = false;
  bool header_seen = false;

  const auto process_line = [&](const std::string &line) {
    if (identities.records >= options.expected_record_count)
      throw ScanError("JSONL record count exceeds expected identity");
    ++identities.records;
    const Json::Value record = parse_json_line(line, identities.records);
    if (identities.records == 1U) {
      identities.header = validate_header(record);
      header_seen = true;
      return;
    }
    if (!header_seen) throw ScanError("callback appeared before run header");
    const std::uint64_t callback_index = identities.records - 2U;
    const CallbackMapping mapping = map_callback(record, callback_index);
    const TurnSafePilotCallbackResult result =
        TurnSafePilotShadow::Evaluate(mapping.input);
    if (!result.accepted())
      throw ScanError("PilotShadow rejected structurally audited callback " +
                      std::to_string(callback_index) + " with status " +
                      ov_msckf::turnsafe_pilot_shadow_status_name(result.status));
    if (result.callback_index != callback_index ||
        !same_double(result.callback_timestamp,
                     mapping.input.callback_timestamp))
      throw ScanError("PilotShadow callback identity changed");
    write_callback_row(csv, options, mapping, result, aggregate);
  };

  const auto process_raw = [&](const char *data, std::size_t size) {
    raw_digest.update(data, size);
    checked_add(identities.raw.size, size, "decoded raw");
    if (identities.raw.size > options.expected_raw_size)
      throw ScanError("decoded raw size exceeds expected identity");
    std::size_t offset = 0U;
    while (offset < size) {
      const void *newline_pointer =
          std::memchr(data + offset, '\n', size - offset);
      const std::size_t segment =
          newline_pointer == nullptr
              ? size - offset
              : static_cast<const char *>(newline_pointer) - (data + offset);
      if (segment > kMaximumJsonlRecordBytes - line_buffer.size())
        throw ScanError("telemetry JSONL record exceeds 256 MiB");
      line_buffer.append(data + offset, segment);
      offset += segment;
      if (newline_pointer != nullptr) {
        if (!line_buffer.empty() && line_buffer.back() == '\r')
          throw ScanError("telemetry JSONL uses noncanonical CRLF termination");
        process_line(line_buffer);
        line_buffer.clear();
        ++offset;
      }
    }
  };

  while (archive) {
    archive.read(compressed.data(),
                 static_cast<std::streamsize>(compressed.size()));
    const std::streamsize count = archive.gcount();
    if (count <= 0) break;
    if (frame_complete)
      throw ScanError("telemetry archive contains trailing or concatenated data");
    const std::size_t input_size = static_cast<std::size_t>(count);
    archive_digest.update(compressed.data(), input_size);
    checked_add(identities.archive.size, input_size, "archive");
    if (identities.archive.size > options.expected_archive_size)
      throw ScanError("archive stream exceeds expected identity");
    ZSTD_inBuffer input{compressed.data(), input_size, 0U};
    while (input.pos < input.size) {
      ZSTD_outBuffer output{decompressed.data(), decompressed.size(), 0U};
      const std::size_t result =
          ZSTD_decompressStream(decoder.get(), &output, &input);
      if (ZSTD_isError(result))
        throw ScanError(std::string("libzstd decompression failed: ") +
                        ZSTD_getErrorName(result));
      if (output.pos != 0U) process_raw(decompressed.data(), output.pos);
      if (result == 0U) {
        frame_complete = true;
        if (input.pos != input.size)
          throw ScanError("telemetry archive contains concatenated frames");
      }
    }
  }
  if (!archive.eof()) throw ScanError("telemetry archive read failed");
  if (!frame_complete) throw ScanError("telemetry zstd frame is truncated");
  if (!line_buffer.empty())
    throw ScanError("telemetry JSONL final record is not newline terminated");
  identities.archive.sha256 = archive_digest.finish();
  identities.raw.sha256 = raw_digest.finish();
  if (identities.archive.size != options.expected_archive_size ||
      identities.archive.sha256 != options.expected_archive_sha256)
    throw ScanError("archive SHA-256/size identity differs");
  if (identities.raw.size != options.expected_raw_size ||
      identities.raw.sha256 != options.expected_raw_sha256)
    throw ScanError("decoded raw SHA-256/size identity differs");
  if (identities.records != options.expected_record_count ||
      aggregate.callback_rows + 1U != identities.records)
    throw ScanError("JSONL record/callback count differs from expected identity");
  return identities;
}

int run_worker(int argc, char **argv) {
  const Options options = parse_options(argc, argv);
  require_regular_archive(options);
  TemporaryOutputs outputs(options.callback_csv, options.metadata_json);
  std::ofstream csv(outputs.csv_temporary(),
                    std::ios::binary | std::ios::trunc);
  if (!csv) throw ScanError("cannot create callback CSV temporary file");
  write_csv_header(csv);
  Aggregate aggregate;
  const StreamIdentities identities = scan_archive(options, csv, aggregate);
  csv.flush();
  if (!csv) throw ScanError("failed to flush callback CSV temporary file");
  csv.close();
  if (!csv) throw ScanError("failed to close callback CSV temporary file");
  const FileIdentity csv_identity = file_identity(outputs.csv_temporary());
  write_metadata(outputs.metadata_temporary(), options, identities.archive,
                 identities.raw, identities.records, csv_identity,
                 identities.header, aggregate);
  outputs.publish();
  std::cout << "turnsafe_p1_shadow_worker COMPLETE callbacks="
            << aggregate.callback_rows << " csv_sha256="
            << csv_identity.sha256 << '\n';
  return 0;
}

} // namespace

int main(int argc, char **argv) {
  try {
    return run_worker(argc, argv);
  } catch (const ScanError &error) {
    std::cerr << "turnsafe_p1_shadow_worker ERROR: " << error.what() << '\n';
    return 1;
  } catch (const std::bad_alloc &) {
    std::cerr << "turnsafe_p1_shadow_worker ERROR: allocation failure\n";
    return 1;
  } catch (const std::exception &error) {
    std::cerr << "turnsafe_p1_shadow_worker ERROR: unexpected exception: "
              << error.what() << '\n';
    return 1;
  } catch (...) {
    std::cerr << "turnsafe_p1_shadow_worker ERROR: unknown exception\n";
    return 1;
  }
}
