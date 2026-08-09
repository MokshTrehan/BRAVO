/*
 * SchurVIO-Lite CP2 composite-state canonical trace codec.
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include "CP2StateTraceCodec.h"

#include "CP2Canonical.h"

#include <algorithm>
#include <array>
#include <cstring>
#include <limits>
#include <string>
#include <tuple>
#include <utility>

namespace ov_msckf {
namespace {

constexpr char kPriorSnapshotDomain[] = "SchurVIO-CP2-prior-snapshot-v1";
constexpr char kPostcommitStateDomain[] = "SchurVIO-CP2-postcommit-state-v1";
constexpr char kStateFileMagic[] = "SchurVIO-CP2-state-file-v1\n\0\0\0\0\0";

static_assert(sizeof(kStateFileMagic) - 1U == 32U,
              "CP2 state file magic must be 32 bytes");
static_assert(sizeof(double) == sizeof(std::uint64_t) &&
                  std::numeric_limits<double>::is_iec559,
              "CP2 state payloads require IEEE-754 binary64");
static_assert(std::numeric_limits<std::int64_t>::is_signed &&
                  std::numeric_limits<std::int64_t>::min() ==
                      -std::numeric_limits<std::int64_t>::max() - 1,
              "CP2 signed metadata requires two's-complement int64");

constexpr std::array<const char *, 8> kSemanticKindStrings{{
    "imu.theta", "imu.position", "imu.velocity", "imu.gyro_bias",
    "imu.accel_bias", "clone.theta", "clone.position", "slam_landmark",
}};
constexpr std::array<const char *, 3> kActiveTypeTagStrings{{
    "imu", "clone", "slam_landmark",
}};
constexpr std::array<const char *, 5> kFixedImuTagStrings{{
    "dw", "da", "tg", "gyro_to_imu", "accel_to_imu",
}};
constexpr std::array<const char *, 6> kLandmarkRepresentationStrings{{
    "GLOBAL_3D",
    "GLOBAL_FULL_INVERSE_DEPTH",
    "ANCHORED_3D",
    "ANCHORED_FULL_INVERSE_DEPTH",
    "ANCHORED_MSCKF_INVERSE_DEPTH",
    "ANCHORED_INVERSE_DEPTH_SINGLE",
}};
constexpr char kCameraModelTag[] = "CamRadtan";

std::uint64_t checked_u64(std::size_t value, const char *what) {
  if (value > std::numeric_limits<std::uint64_t>::max()) {
    throw CP2TraceCodecError(std::string(what) + " exceeds u64");
  }
  return static_cast<std::uint64_t>(value);
}

std::string sha256_hex(const std::vector<std::uint8_t> &bytes) {
  CP2Sha256 sha256;
  sha256.Update(bytes);
  return sha256.HexDigest();
}

bool valid_utf8(const std::string &value) noexcept {
  const auto *bytes = reinterpret_cast<const unsigned char *>(value.data());
  std::size_t index = 0U;
  while (index < value.size()) {
    const unsigned char first = bytes[index];
    if (first <= 0x7fU) {
      ++index;
      continue;
    }
    if (first >= 0xc2U && first <= 0xdfU) {
      if (index + 1U >= value.size() || bytes[index + 1U] < 0x80U ||
          bytes[index + 1U] > 0xbfU) {
        return false;
      }
      index += 2U;
      continue;
    }
    if (first >= 0xe0U && first <= 0xefU) {
      if (index + 2U >= value.size()) {
        return false;
      }
      const unsigned char second = bytes[index + 1U];
      const unsigned char third = bytes[index + 2U];
      if (third < 0x80U || third > 0xbfU ||
          (first == 0xe0U ? (second < 0xa0U || second > 0xbfU)
                          : first == 0xedU ? (second < 0x80U || second > 0x9fU)
                                           : (second < 0x80U || second > 0xbfU))) {
        return false;
      }
      index += 3U;
      continue;
    }
    if (first >= 0xf0U && first <= 0xf4U) {
      if (index + 3U >= value.size()) {
        return false;
      }
      const unsigned char second = bytes[index + 1U];
      const unsigned char third = bytes[index + 2U];
      const unsigned char fourth = bytes[index + 3U];
      if (third < 0x80U || third > 0xbfU || fourth < 0x80U || fourth > 0xbfU ||
          (first == 0xf0U ? (second < 0x90U || second > 0xbfU)
                          : first == 0xf4U ? (second < 0x80U || second > 0x8fU)
                                           : (second < 0x80U || second > 0xbfU))) {
        return false;
      }
      index += 4U;
      continue;
    }
    return false;
  }
  return true;
}

class StateCanonicalReader {
public:
  StateCanonicalReader(const std::vector<std::uint8_t> &bytes,
                       const CP2TraceDecodeLimits &limits,
                       bool enforce_payload_limit,
                       std::uint64_t &aggregate_coefficients)
      : bytes_(bytes), limits_(limits), aggregate_coefficients_(aggregate_coefficients) {
    if (enforce_payload_limit &&
        checked_u64(bytes.size(), "state payload size") > limits.maximum_payload_bytes) {
      throw CP2TraceCodecError("CP2 state payload exceeds configured byte limit");
    }
  }

  std::size_t position() const noexcept { return position_; }
  std::size_t remaining() const noexcept { return bytes_.size() - position_; }
  bool empty() const noexcept { return position_ == bytes_.size(); }

  void Expect(const void *expected, std::size_t size, const char *what) {
    Require(size, what);
    if (size > 0U && std::memcmp(bytes_.data() + position_, expected, size) != 0) {
      throw CP2TraceCodecError(std::string("invalid ") + what);
    }
    position_ += size;
  }

  std::uint8_t ReadU8(const char *what) {
    Require(1U, what);
    return bytes_[position_++];
  }

  std::uint64_t ReadU64(const char *what) {
    Require(8U, what);
    std::uint64_t value = 0U;
    for (std::size_t index = 0U; index < 8U; ++index) {
      value = (value << 8U) | bytes_[position_ + index];
    }
    position_ += 8U;
    return value;
  }

  std::int64_t ReadI64(const char *what) {
    const std::uint64_t bits = ReadU64(what);
    std::int64_t value = 0;
    static_assert(sizeof(value) == sizeof(bits), "CP2 requires 64-bit signed integers");
    std::memcpy(&value, &bits, sizeof(value));
    return value;
  }

  void ReadBinary64(double &value, const char *what) {
    const std::uint64_t bits = ReadU64(what);
    static_assert(sizeof(value) == sizeof(bits), "CP2 requires IEEE-754 binary64");
    std::memcpy(&value, &bits, sizeof(value));
  }

  std::string ReadUtf8(const char *what) {
    const std::uint64_t count = ReadU64(what);
    if (count > limits_.maximum_payload_bytes ||
        count > std::numeric_limits<std::size_t>::max()) {
      throw CP2TraceCodecError(std::string(what) + " exceeds configured byte limit");
    }
    const std::size_t size = static_cast<std::size_t>(count);
    Require(size, what);
    std::string value(reinterpret_cast<const char *>(bytes_.data() + position_), size);
    position_ += size;
    if (!valid_utf8(value)) {
      throw CP2TraceCodecError(std::string(what) + " is not valid UTF-8");
    }
    return value;
  }

  Eigen::MatrixXd ReadMatrix(const char *what) {
    const std::uint64_t rows = ReadU64(what);
    const std::uint64_t columns = ReadU64(what);
    const std::uint64_t coefficients = CheckedCoefficientCount(rows, columns, what);
    ConsumeCoefficients(coefficients, what);
    RequireCoefficientBytes(coefficients, what);
    Eigen::MatrixXd result(CheckedEigenIndex(rows, what), CheckedEigenIndex(columns, what));
    for (Eigen::Index row = 0; row < result.rows(); ++row) {
      for (Eigen::Index column = 0; column < result.cols(); ++column) {
        ReadBinary64(result.coeffRef(row, column), what);
      }
    }
    return result;
  }

  std::uint64_t ReadListCount(const char *what, std::size_t minimum_entry_bytes) {
    const std::uint64_t count = ReadU64(what);
    if (count > limits_.maximum_layout_blocks ||
        count > std::numeric_limits<std::size_t>::max() ||
        (minimum_entry_bytes > 0U &&
         count > static_cast<std::uint64_t>(remaining() / minimum_entry_bytes))) {
      throw CP2TraceCodecError(std::string(what) + " exceeds configured or retained bounds");
    }
    return count;
  }

  std::vector<std::uint8_t> ReadBytes(std::uint64_t count, const char *what) {
    if (count > limits_.maximum_payload_bytes ||
        count > std::numeric_limits<std::size_t>::max()) {
      throw CP2TraceCodecError(std::string(what) + " exceeds configured byte limit");
    }
    const std::size_t size = static_cast<std::size_t>(count);
    Require(size, what);
    std::vector<std::uint8_t> result(
        bytes_.begin() + static_cast<std::ptrdiff_t>(position_),
        bytes_.begin() + static_cast<std::ptrdiff_t>(position_ + size));
    position_ += size;
    return result;
  }

private:
  void Require(std::size_t size, const char *what) const {
    if (size > bytes_.size() - position_) {
      throw CP2TraceCodecError(std::string("truncated ") + what);
    }
  }

  Eigen::Index CheckedEigenIndex(std::uint64_t value, const char *what) const {
    if (value > static_cast<std::uintmax_t>(std::numeric_limits<Eigen::Index>::max())) {
      throw CP2TraceCodecError(std::string(what) + " dimension exceeds Eigen::Index");
    }
    return static_cast<Eigen::Index>(value);
  }

  std::uint64_t CheckedCoefficientCount(std::uint64_t rows, std::uint64_t columns,
                                        const char *what) const {
    if (rows != 0U && columns > std::numeric_limits<std::uint64_t>::max() / rows) {
      throw CP2TraceCodecError(std::string(what) + " coefficient count overflows u64");
    }
    const std::uint64_t coefficients = rows * columns;
    if (coefficients > limits_.maximum_matrix_coefficients) {
      throw CP2TraceCodecError(std::string(what) + " exceeds configured coefficient limit");
    }
    CheckedEigenIndex(rows, what);
    CheckedEigenIndex(columns, what);
    return coefficients;
  }

  void ConsumeCoefficients(std::uint64_t coefficients, const char *what) {
    const std::uint64_t limit = limits_.maximum_total_matrix_coefficients;
    if (coefficients > limit || aggregate_coefficients_ > limit - coefficients) {
      throw CP2TraceCodecError(std::string(what) +
                               " exceeds configured aggregate coefficient limit");
    }
    aggregate_coefficients_ += coefficients;
  }

  void RequireCoefficientBytes(std::uint64_t coefficients, const char *what) const {
    if (coefficients > std::numeric_limits<std::size_t>::max() / 8U) {
      throw CP2TraceCodecError(std::string(what) + " byte count exceeds size_t");
    }
    Require(static_cast<std::size_t>(coefficients) * 8U, what);
  }

  const std::vector<std::uint8_t> &bytes_;
  const CP2TraceDecodeLimits &limits_;
  std::uint64_t &aggregate_coefficients_;
  std::size_t position_ = 0U;
};

template <typename Enum, std::size_t Size>
const char *enum_string(Enum value, const std::array<const char *, Size> &spellings,
                        const char *what) {
  const std::uint64_t index = static_cast<std::uint64_t>(value);
  if (index >= Size) {
    throw CP2TraceCodecError(std::string("invalid ") + what);
  }
  return spellings[static_cast<std::size_t>(index)];
}

template <typename Enum, std::size_t Size>
Enum parse_enum(const std::string &value,
                const std::array<const char *, Size> &spellings,
                const char *what) {
  for (std::size_t index = 0U; index < Size; ++index) {
    if (value == spellings[index]) {
      return static_cast<Enum>(index);
    }
  }
  throw CP2TraceCodecError(std::string("unknown ") + what);
}

std::uint8_t phase_value(CP2StatePhase phase) {
  switch (phase) {
  case CP2StatePhase::kPhase0Prior:
    return 0U;
  case CP2StatePhase::kPhase1Precommit:
    return 1U;
  case CP2StatePhase::kPhase2ExpectedPostcommit:
    return 2U;
  case CP2StatePhase::kPhase3LivePostcommit:
    return 3U;
  }
  throw CP2TraceCodecError("invalid CP2 state phase");
}

CP2StatePhase parse_phase(std::uint8_t value) {
  switch (value) {
  case 0U:
    return CP2StatePhase::kPhase0Prior;
  case 1U:
    return CP2StatePhase::kPhase1Precommit;
  case 2U:
    return CP2StatePhase::kPhase2ExpectedPostcommit;
  case 3U:
    return CP2StatePhase::kPhase3LivePostcommit;
  default:
    throw CP2TraceCodecError("invalid CP2 state frame phase");
  }
}

bool is_prior_phase(CP2StatePhase phase) {
  const std::uint8_t value = phase_value(phase);
  return value == 0U || value == 1U;
}

void append_domain(CP2CanonicalBuffer &output, CP2StatePhase phase) {
  if (is_prior_phase(phase)) {
    output.AppendRawBytes(kPriorSnapshotDomain, sizeof(kPriorSnapshotDomain));
  } else {
    output.AppendRawBytes(kPostcommitStateDomain, sizeof(kPostcommitStateDomain));
  }
}

void expect_domain(StateCanonicalReader &input, CP2StatePhase phase) {
  if (is_prior_phase(phase)) {
    input.Expect(kPriorSnapshotDomain, sizeof(kPriorSnapshotDomain),
                 "prior-snapshot domain");
  } else {
    input.Expect(kPostcommitStateDomain, sizeof(kPostcommitStateDomain),
                 "postcommit-state domain");
  }
}

void append_landmark_identity(CP2CanonicalBuffer &output,
                              const CP2LandmarkIdentity &identity) {
  output.AppendU64(identity.feature_id);
  output.AppendUtf8(enum_string(identity.representation,
                               kLandmarkRepresentationStrings,
                               "landmark representation"));
  output.AppendI64(identity.anchor_camera_id);
  output.AppendBinary64(identity.anchor_timestamp);
}

CP2LandmarkIdentity read_landmark_identity(StateCanonicalReader &input) {
  CP2LandmarkIdentity identity;
  identity.feature_id = input.ReadU64("landmark feature ID");
  identity.representation =
      parse_enum<CP2LandmarkRepresentation>(
          input.ReadUtf8("landmark representation"),
          kLandmarkRepresentationStrings, "landmark representation");
  identity.anchor_camera_id = input.ReadI64("landmark anchor camera ID");
  input.ReadBinary64(identity.anchor_timestamp, "landmark anchor timestamp");
  return identity;
}

bool semantic_is_clone(CP2SemanticStateKind kind) {
  const std::uint64_t value = static_cast<std::uint64_t>(kind);
  if (value >= kSemanticKindStrings.size()) {
    throw CP2TraceCodecError("invalid semantic state kind");
  }
  return value == 5U || value == 6U;
}

bool semantic_is_landmark(CP2SemanticStateKind kind) {
  const std::uint64_t value = static_cast<std::uint64_t>(kind);
  if (value >= kSemanticKindStrings.size()) {
    throw CP2TraceCodecError("invalid semantic state kind");
  }
  return value == 7U;
}

bool active_is_clone(CP2ActiveStateTypeTag tag) {
  const std::uint64_t value = static_cast<std::uint64_t>(tag);
  if (value >= kActiveTypeTagStrings.size()) {
    throw CP2TraceCodecError("invalid active state type tag");
  }
  return value == 1U;
}

bool active_is_landmark(CP2ActiveStateTypeTag tag) {
  const std::uint64_t value = static_cast<std::uint64_t>(tag);
  if (value >= kActiveTypeTagStrings.size()) {
    throw CP2TraceCodecError("invalid active state type tag");
  }
  return value == 2U;
}

void encode_snapshot_body(CP2CanonicalBuffer &output,
                          const CP2CompositeStateSnapshot &snapshot) {
  output.AppendBinary64(snapshot.timestamp);
  output.AppendMatrix(snapshot.covariance);

  output.AppendU64(checked_u64(snapshot.semantic_blocks.size(),
                               "semantic-block count"));
  for (const CP2SemanticStateBlock &block : snapshot.semantic_blocks) {
    output.AppendUtf8(enum_string(block.kind, kSemanticKindStrings,
                                 "semantic state kind"));
    output.AppendU64(block.covariance_id);
    output.AppendU64(block.error_size);
    if (semantic_is_clone(block.kind)) {
      output.AppendBinary64(block.clone_timestamp);
    } else if (semantic_is_landmark(block.kind)) {
      append_landmark_identity(output, block.landmark);
    }
  }

  output.AppendU64(checked_u64(snapshot.active_types.size(), "active-type count"));
  for (const CP2ActiveStateType &type : snapshot.active_types) {
    output.AppendUtf8(enum_string(type.tag, kActiveTypeTagStrings,
                                 "active state type tag"));
    output.AppendU64(type.covariance_id);
    output.AppendU64(type.error_size);
    if (active_is_clone(type.tag)) {
      output.AppendBinary64(type.clone_timestamp);
    } else if (active_is_landmark(type.tag)) {
      append_landmark_identity(output, type.landmark);
    }
    output.AppendMatrix(type.nominal);
    output.AppendMatrix(type.fej);
  }

  output.AppendU64(checked_u64(snapshot.fixed_imu_calibrations.size(),
                               "fixed IMU calibration count"));
  for (const CP2FixedImuCalibration &calibration :
       snapshot.fixed_imu_calibrations) {
    output.AppendUtf8(enum_string(calibration.tag, kFixedImuTagStrings,
                                 "fixed IMU calibration tag"));
    output.AppendMatrix(calibration.nominal);
    output.AppendMatrix(calibration.fej);
  }

  output.AppendMatrix(snapshot.time_offset_nominal);
  output.AppendMatrix(snapshot.time_offset_fej);

  output.AppendU64(checked_u64(snapshot.camera_calibrations.size(),
                               "camera-calibration count"));
  for (const CP2CameraFixedCalibration &calibration : snapshot.camera_calibrations) {
    output.AppendU64(calibration.camera_id);
    output.AppendMatrix(calibration.extrinsic_nominal);
    output.AppendMatrix(calibration.extrinsic_fej);
    output.AppendMatrix(calibration.intrinsic_nominal);
    output.AppendMatrix(calibration.intrinsic_fej);
  }

  output.AppendU64(checked_u64(snapshot.camera_caches.size(), "camera-cache count"));
  for (const CP2CameraModelCache &cache : snapshot.camera_caches) {
    output.AppendU64(cache.camera_id);
    output.AppendUtf8(kCameraModelTag);
    output.AppendI64(cache.width);
    output.AppendI64(cache.height);
    output.AppendMatrix(cache.calibration);
    output.AppendMatrix(cache.K);
    output.AppendMatrix(cache.D);
  }
}

CP2CompositeStateSnapshot decode_snapshot_payload(
    const std::vector<std::uint8_t> &payload,
    CP2StatePhase phase,
    const CP2TraceDecodeLimits &limits,
    std::uint64_t &aggregate_coefficients) {
  StateCanonicalReader input(payload, limits, true, aggregate_coefficients);
  expect_domain(input, phase);

  CP2CompositeStateSnapshot snapshot;
  snapshot.phase = phase;
  input.ReadBinary64(snapshot.timestamp, "state timestamp");
  snapshot.covariance = input.ReadMatrix("state covariance");

  const std::uint64_t semantic_count =
      input.ReadListCount("semantic-block count", 33U);
  snapshot.semantic_blocks.reserve(static_cast<std::size_t>(semantic_count));
  for (std::uint64_t index = 0U; index < semantic_count; ++index) {
    CP2SemanticStateBlock block;
    block.kind = parse_enum<CP2SemanticStateKind>(
        input.ReadUtf8("semantic state kind"), kSemanticKindStrings,
        "semantic state kind");
    block.covariance_id = input.ReadU64("semantic covariance ID");
    block.error_size = input.ReadU64("semantic error size");
    if (semantic_is_clone(block.kind)) {
      input.ReadBinary64(block.clone_timestamp, "semantic clone timestamp");
    } else if (semantic_is_landmark(block.kind)) {
      block.landmark = read_landmark_identity(input);
    }
    snapshot.semantic_blocks.push_back(std::move(block));
  }

  const std::uint64_t active_count = input.ReadListCount("active-type count", 59U);
  snapshot.active_types.reserve(static_cast<std::size_t>(active_count));
  for (std::uint64_t index = 0U; index < active_count; ++index) {
    CP2ActiveStateType type;
    type.tag = parse_enum<CP2ActiveStateTypeTag>(
        input.ReadUtf8("active state type tag"), kActiveTypeTagStrings,
        "active state type tag");
    type.covariance_id = input.ReadU64("active covariance ID");
    type.error_size = input.ReadU64("active error size");
    if (active_is_clone(type.tag)) {
      input.ReadBinary64(type.clone_timestamp, "active clone timestamp");
    } else if (active_is_landmark(type.tag)) {
      type.landmark = read_landmark_identity(input);
    }
    type.nominal = input.ReadMatrix("active nominal");
    type.fej = input.ReadMatrix("active FEJ");
    snapshot.active_types.push_back(std::move(type));
  }

  const std::uint64_t fixed_count =
      input.ReadListCount("fixed IMU calibration count", 42U);
  snapshot.fixed_imu_calibrations.reserve(static_cast<std::size_t>(fixed_count));
  for (std::uint64_t index = 0U; index < fixed_count; ++index) {
    CP2FixedImuCalibration calibration;
    calibration.tag = parse_enum<CP2FixedImuCalibrationTag>(
        input.ReadUtf8("fixed IMU calibration tag"), kFixedImuTagStrings,
        "fixed IMU calibration tag");
    calibration.nominal = input.ReadMatrix("fixed IMU calibration nominal");
    calibration.fej = input.ReadMatrix("fixed IMU calibration FEJ");
    snapshot.fixed_imu_calibrations.push_back(std::move(calibration));
  }

  snapshot.time_offset_nominal = input.ReadMatrix("time-offset nominal");
  snapshot.time_offset_fej = input.ReadMatrix("time-offset FEJ");

  const std::uint64_t camera_calibration_count =
      input.ReadListCount("camera-calibration count", 72U);
  snapshot.camera_calibrations.reserve(
      static_cast<std::size_t>(camera_calibration_count));
  for (std::uint64_t index = 0U; index < camera_calibration_count; ++index) {
    CP2CameraFixedCalibration calibration;
    calibration.camera_id = input.ReadU64("camera-calibration camera ID");
    calibration.extrinsic_nominal = input.ReadMatrix("camera extrinsic nominal");
    calibration.extrinsic_fej = input.ReadMatrix("camera extrinsic FEJ");
    calibration.intrinsic_nominal = input.ReadMatrix("camera intrinsic nominal");
    calibration.intrinsic_fej = input.ReadMatrix("camera intrinsic FEJ");
    snapshot.camera_calibrations.push_back(std::move(calibration));
  }

  const std::uint64_t camera_cache_count =
      input.ReadListCount("camera-cache count", 89U);
  snapshot.camera_caches.reserve(static_cast<std::size_t>(camera_cache_count));
  for (std::uint64_t index = 0U; index < camera_cache_count; ++index) {
    CP2CameraModelCache cache;
    cache.camera_id = input.ReadU64("camera-cache camera ID");
    if (input.ReadUtf8("camera model tag") != kCameraModelTag) {
      throw CP2TraceCodecError("unknown camera model tag");
    }
    cache.width = input.ReadI64("camera-cache width");
    cache.height = input.ReadI64("camera-cache height");
    cache.calibration = input.ReadMatrix("camera-cache calibration");
    cache.K = input.ReadMatrix("camera-cache K");
    cache.D = input.ReadMatrix("camera-cache D");
    snapshot.camera_caches.push_back(std::move(cache));
  }

  if (!input.empty()) {
    throw CP2TraceCodecError("state snapshot payload has trailing bytes");
  }
  return snapshot;
}

using StateFrameKey =
    std::tuple<std::uint64_t, std::uint64_t, std::uint64_t, std::uint8_t>;

StateFrameKey state_frame_key(const CP2StateTraceFrame &frame) {
  return std::make_tuple(frame.invocation.sequence_index, frame.invocation.pair_index,
                         frame.invocation.invocation_id, phase_value(frame.phase));
}

void validate_state_frame_sequence(const std::vector<CP2StateTraceFrame> &frames) {
  bool have_previous = false;
  StateFrameKey previous;
  for (const CP2StateTraceFrame &frame : frames) {
    if (frame.phase != frame.snapshot.phase) {
      throw CP2TraceCodecError("state frame phase does not match snapshot phase");
    }
    const StateFrameKey key = state_frame_key(frame);
    if (have_previous && !(previous < key)) {
      throw CP2TraceCodecError("state frames are not in strict lexicographic order");
    }
    previous = key;
    have_previous = true;
  }

  std::size_t begin = 0U;
  while (begin < frames.size()) {
    std::size_t end = begin + 1U;
    while (end < frames.size() &&
           frames[end].invocation == frames[begin].invocation) {
      ++end;
    }
    const std::size_t count = end - begin;
    if (count != 2U && count != 4U) {
      throw CP2TraceCodecError("state invocation has an illegal phase population");
    }
    for (std::size_t offset = 0U; offset < count; ++offset) {
      if (phase_value(frames[begin + offset].phase) != offset) {
        throw CP2TraceCodecError("state invocation phases are not exactly [0,1] or [0,1,2,3]");
      }
    }
    begin = end;
  }
}

} // namespace

std::vector<std::uint8_t> CP2StateTraceCodec::EncodeSnapshotPayload(
    const CP2CompositeStateSnapshot &snapshot) {
  // Validate the enum even if the selected branch would otherwise be implicit.
  phase_value(snapshot.phase);
  CP2CanonicalBuffer output;
  append_domain(output, snapshot.phase);
  encode_snapshot_body(output, snapshot);
  return output.bytes();
}

CP2CompositeStateSnapshot CP2StateTraceCodec::DecodeSnapshotPayload(
    const std::vector<std::uint8_t> &payload,
    CP2StatePhase phase,
    const CP2TraceDecodeLimits &limits) {
  std::uint64_t aggregate_coefficients = 0U;
  return decode_snapshot_payload(payload, phase, limits, aggregate_coefficients);
}

CP2EncodedStateFile CP2StateTraceCodec::EncodeStateFile(
    const std::vector<CP2StateTraceFrame> &frames) {
  validate_state_frame_sequence(frames);
  CP2CanonicalBuffer output;
  output.AppendRawBytes(kStateFileMagic, sizeof(kStateFileMagic) - 1U);
  CP2EncodedStateFile encoded;
  encoded.payloads.reserve(frames.size());

  for (const CP2StateTraceFrame &frame : frames) {
    const std::vector<std::uint8_t> payload = EncodeSnapshotPayload(frame.snapshot);
    const std::uint8_t phase = phase_value(frame.phase);
    output.AppendRawBytes(&phase, 1U);
    const std::uint8_t reserved[7] = {0U, 0U, 0U, 0U, 0U, 0U, 0U};
    output.AppendRawBytes(reserved, sizeof(reserved));
    output.AppendU64(frame.invocation.sequence_index);
    output.AppendU64(frame.invocation.pair_index);
    output.AppendU64(frame.invocation.invocation_id);
    output.AppendU64(checked_u64(payload.size(), "state payload length"));

    CP2TracePayloadReference reference;
    reference.offset = checked_u64(output.size(), "state payload offset");
    reference.length = checked_u64(payload.size(), "state payload length");
    if (reference.length >
        std::numeric_limits<std::uint64_t>::max() - reference.offset) {
      throw CP2TraceCodecError("state payload offset plus length overflows u64");
    }
    reference.sha256 = sha256_hex(payload);
    encoded.payloads.push_back(reference);
    output.AppendRawBytes(payload);
  }
  encoded.bytes = output.bytes();
  return encoded;
}

std::vector<CP2StateTraceFrame> CP2StateTraceCodec::DecodeStateFile(
    const std::vector<std::uint8_t> &file,
    const CP2TraceDecodeLimits &limits) {
  if (checked_u64(file.size(), "state file size") > limits.maximum_file_bytes) {
    throw CP2TraceCodecError("state file exceeds configured aggregate byte limit");
  }
  std::uint64_t aggregate_coefficients = 0U;
  StateCanonicalReader input(file, limits, false, aggregate_coefficients);
  input.Expect(kStateFileMagic, sizeof(kStateFileMagic) - 1U, "state file domain");

  std::vector<CP2StateTraceFrame> frames;
  while (!input.empty()) {
    if (frames.size() >= limits.maximum_frames) {
      throw CP2TraceCodecError("state frame count exceeds configured limit");
    }
    CP2StateTraceFrame frame;
    frame.phase = parse_phase(input.ReadU8("state frame phase"));
    const std::uint8_t reserved[7] = {0U, 0U, 0U, 0U, 0U, 0U, 0U};
    input.Expect(reserved, sizeof(reserved), "state frame reserved bytes");
    frame.invocation.sequence_index = input.ReadU64("state frame sequence index");
    frame.invocation.pair_index = input.ReadU64("state frame pair index");
    frame.invocation.invocation_id = input.ReadU64("state frame invocation ID");
    const std::uint64_t payload_length = input.ReadU64("state frame payload length");
    frame.payload.offset = checked_u64(input.position(), "state payload offset");
    frame.payload.length = payload_length;
    if (frame.payload.length >
        std::numeric_limits<std::uint64_t>::max() - frame.payload.offset) {
      throw CP2TraceCodecError("state payload offset plus length overflows u64");
    }
    const std::vector<std::uint8_t> payload =
        input.ReadBytes(payload_length, "state frame payload");
    frame.payload.sha256 = sha256_hex(payload);
    frame.snapshot =
        decode_snapshot_payload(payload, frame.phase, limits, aggregate_coefficients);
    frames.push_back(std::move(frame));
  }
  validate_state_frame_sequence(frames);
  return frames;
}

} // namespace ov_msckf
