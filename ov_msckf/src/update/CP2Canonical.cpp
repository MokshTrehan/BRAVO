/*
 * SchurVIO-Lite CP2 canonical serialization support.
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include "CP2Canonical.h"

#include <algorithm>
#include <cstring>
#include <functional>
#include <limits>

namespace ov_msckf {
namespace {

constexpr std::array<std::uint32_t, 64> kRoundConstants{{
    0x428a2f98U, 0x71374491U, 0xb5c0fbcfU, 0xe9b5dba5U, 0x3956c25bU, 0x59f111f1U, 0x923f82a4U,
    0xab1c5ed5U, 0xd807aa98U, 0x12835b01U, 0x243185beU, 0x550c7dc3U, 0x72be5d74U, 0x80deb1feU,
    0x9bdc06a7U, 0xc19bf174U, 0xe49b69c1U, 0xefbe4786U, 0x0fc19dc6U, 0x240ca1ccU, 0x2de92c6fU,
    0x4a7484aaU, 0x5cb0a9dcU, 0x76f988daU, 0x983e5152U, 0xa831c66dU, 0xb00327c8U, 0xbf597fc7U,
    0xc6e00bf3U, 0xd5a79147U, 0x06ca6351U, 0x14292967U, 0x27b70a85U, 0x2e1b2138U, 0x4d2c6dfcU,
    0x53380d13U, 0x650a7354U, 0x766a0abbU, 0x81c2c92eU, 0x92722c85U, 0xa2bfe8a1U, 0xa81a664bU,
    0xc24b8b70U, 0xc76c51a3U, 0xd192e819U, 0xd6990624U, 0xf40e3585U, 0x106aa070U, 0x19a4c116U,
    0x1e376c08U, 0x2748774cU, 0x34b0bcb5U, 0x391c0cb3U, 0x4ed8aa4aU, 0x5b9cca4fU, 0x682e6ff3U,
    0x748f82eeU, 0x78a5636fU, 0x84c87814U, 0x8cc70208U, 0x90befffaU, 0xa4506cebU, 0xbef9a3f7U,
    0xc67178f2U,
}};

std::uint32_t RotateRight(std::uint32_t value, unsigned int count) noexcept {
  return (value >> count) | (value << (32U - count));
}

std::uint32_t LoadU32BigEndian(const std::uint8_t *bytes) noexcept {
  return (static_cast<std::uint32_t>(bytes[0]) << 24U) | (static_cast<std::uint32_t>(bytes[1]) << 16U) |
         (static_cast<std::uint32_t>(bytes[2]) << 8U) | static_cast<std::uint32_t>(bytes[3]);
}

void StoreU32BigEndian(std::uint32_t value, std::uint8_t *bytes) noexcept {
  bytes[0] = static_cast<std::uint8_t>(value >> 24U);
  bytes[1] = static_cast<std::uint8_t>(value >> 16U);
  bytes[2] = static_cast<std::uint8_t>(value >> 8U);
  bytes[3] = static_cast<std::uint8_t>(value);
}

} // namespace

CP2Sha256::CP2Sha256() noexcept
    : state_{{0x6a09e667U, 0xbb67ae85U, 0x3c6ef372U, 0xa54ff53aU, 0x510e527fU, 0x9b05688cU,
              0x1f83d9abU, 0x5be0cd19U}},
      buffer_{{0}}, buffer_size_(0), total_bytes_(0) {}

void CP2Sha256::Update(const void *data, std::size_t size) {
  if (size > 0U && data == nullptr) {
    throw std::invalid_argument("CP2 SHA-256 input pointer is null");
  }
  constexpr std::uint64_t kMaximumMessageBytes = std::numeric_limits<std::uint64_t>::max() / 8U;
  if (size > kMaximumMessageBytes - total_bytes_) {
    throw std::length_error("CP2 SHA-256 input exceeds the 64-bit bit-length field");
  }
  total_bytes_ += static_cast<std::uint64_t>(size);

  const auto *input = static_cast<const std::uint8_t *>(data);
  std::size_t remaining = size;
  while (remaining > 0U) {
    const std::size_t available = buffer_.size() - buffer_size_;
    const std::size_t copied = std::min(available, remaining);
    std::memcpy(buffer_.data() + buffer_size_, input, copied);
    buffer_size_ += copied;
    input += copied;
    remaining -= copied;
    if (buffer_size_ == buffer_.size()) {
      Transform(buffer_.data());
      buffer_size_ = 0U;
    }
  }
}

void CP2Sha256::Update(const std::vector<std::uint8_t> &bytes) { Update(bytes.data(), bytes.size()); }

void CP2Sha256::Update(const std::string &bytes) { Update(bytes.data(), bytes.size()); }

void CP2Sha256::Transform(const std::uint8_t block[64]) noexcept {
  std::array<std::uint32_t, 64> words{{0}};
  for (std::size_t index = 0; index < 16U; ++index) {
    words[index] = LoadU32BigEndian(block + 4U * index);
  }
  for (std::size_t index = 16U; index < words.size(); ++index) {
    const std::uint32_t s0 = RotateRight(words[index - 15U], 7U) ^ RotateRight(words[index - 15U], 18U) ^
                             (words[index - 15U] >> 3U);
    const std::uint32_t s1 = RotateRight(words[index - 2U], 17U) ^ RotateRight(words[index - 2U], 19U) ^
                             (words[index - 2U] >> 10U);
    words[index] = words[index - 16U] + s0 + words[index - 7U] + s1;
  }

  std::uint32_t a = state_[0];
  std::uint32_t b = state_[1];
  std::uint32_t c = state_[2];
  std::uint32_t d = state_[3];
  std::uint32_t e = state_[4];
  std::uint32_t f = state_[5];
  std::uint32_t g = state_[6];
  std::uint32_t h = state_[7];

  for (std::size_t index = 0; index < words.size(); ++index) {
    const std::uint32_t sum1 = RotateRight(e, 6U) ^ RotateRight(e, 11U) ^ RotateRight(e, 25U);
    const std::uint32_t choice = (e & f) ^ ((~e) & g);
    const std::uint32_t temporary1 = h + sum1 + choice + kRoundConstants[index] + words[index];
    const std::uint32_t sum0 = RotateRight(a, 2U) ^ RotateRight(a, 13U) ^ RotateRight(a, 22U);
    const std::uint32_t majority = (a & b) ^ (a & c) ^ (b & c);
    const std::uint32_t temporary2 = sum0 + majority;

    h = g;
    g = f;
    f = e;
    e = d + temporary1;
    d = c;
    c = b;
    b = a;
    a = temporary1 + temporary2;
  }

  state_[0] += a;
  state_[1] += b;
  state_[2] += c;
  state_[3] += d;
  state_[4] += e;
  state_[5] += f;
  state_[6] += g;
  state_[7] += h;
}

std::array<std::uint8_t, 32> CP2Sha256::FinalizeCopy() noexcept {
  const std::uint64_t message_bits = total_bytes_ * 8U;
  buffer_[buffer_size_++] = 0x80U;
  if (buffer_size_ > 56U) {
    std::fill(buffer_.begin() + static_cast<std::ptrdiff_t>(buffer_size_), buffer_.end(), 0U);
    Transform(buffer_.data());
    buffer_size_ = 0U;
  }
  std::fill(buffer_.begin() + static_cast<std::ptrdiff_t>(buffer_size_), buffer_.begin() + 56, 0U);
  for (std::size_t index = 0; index < 8U; ++index) {
    buffer_[56U + index] = static_cast<std::uint8_t>(message_bits >> (56U - 8U * index));
  }
  Transform(buffer_.data());

  std::array<std::uint8_t, 32> digest{{0}};
  for (std::size_t index = 0; index < state_.size(); ++index) {
    StoreU32BigEndian(state_[index], digest.data() + 4U * index);
  }
  return digest;
}

std::array<std::uint8_t, 32> CP2Sha256::Digest() const noexcept {
  CP2Sha256 copy = *this;
  return copy.FinalizeCopy();
}

std::string CP2Sha256::HexDigest() const {
  static constexpr char kHexDigits[] = "0123456789abcdef";
  const std::array<std::uint8_t, 32> digest = Digest();
  std::string result;
  result.resize(2U * digest.size());
  for (std::size_t index = 0; index < digest.size(); ++index) {
    result[2U * index] = kHexDigits[digest[index] >> 4U];
    result[2U * index + 1U] = kHexDigits[digest[index] & 0x0fU];
  }
  return result;
}

void CP2CanonicalBuffer::AppendRawBytes(const void *data, std::size_t size) {
  if (size > 0U && data == nullptr) {
    throw std::invalid_argument("CP2 canonical input pointer is null");
  }
  if (size == 0U) {
    return;
  }
  if (size > bytes_.max_size() - bytes_.size()) {
    throw std::length_error("CP2 canonical buffer size overflow");
  }
  const auto *first = static_cast<const std::uint8_t *>(data);
  const auto *append_first = first;
  std::vector<std::uint8_t> staged;
  if (!bytes_.empty()) {
    // std::less<const void *> supplies the defined total pointer order needed
    // to detect overlap even when the caller's valid range is unrelated to
    // bytes_. Stage only the rare aliased case; ordinary scalar/matrix appends
    // must not allocate a temporary for every coefficient.
    const auto *last = first + size;
    const auto *storage_first = bytes_.data();
    const auto *storage_last = storage_first + bytes_.size();
    const std::less<const void *> less;
    const bool overlaps = less(first, storage_last) && less(storage_first, last);
    if (overlaps) {
      staged.assign(first, last);
      append_first = staged.data();
    }
  }
  CP2Sha256 updated_sha256 = sha256_;
  updated_sha256.Update(append_first, size);
  bytes_.insert(bytes_.end(), append_first, append_first + size);
  sha256_ = updated_sha256;
}

void CP2CanonicalBuffer::AppendRawBytes(const std::vector<std::uint8_t> &bytes) {
  AppendRawBytes(bytes.data(), bytes.size());
}

void CP2CanonicalBuffer::AppendRawBytes(const std::string &bytes) { AppendRawBytes(bytes.data(), bytes.size()); }

void CP2CanonicalBuffer::AppendU64(std::uint64_t value) {
  std::array<std::uint8_t, 8> encoded{{0}};
  for (std::size_t index = 0; index < encoded.size(); ++index) {
    encoded[index] = static_cast<std::uint8_t>(value >> (56U - 8U * index));
  }
  AppendRawBytes(encoded.data(), encoded.size());
}

void CP2CanonicalBuffer::AppendI64(std::int64_t value) { AppendU64(static_cast<std::uint64_t>(value)); }

void CP2CanonicalBuffer::AppendBinary64(double value) {
  static_assert(sizeof(double) == sizeof(std::uint64_t), "CP2 requires 64-bit double");
  static_assert(std::numeric_limits<double>::is_iec559, "CP2 requires IEEE-754 binary64");
  std::uint64_t bits = 0U;
  std::memcpy(&bits, &value, sizeof(bits));
  AppendU64(bits);
}

bool CP2CanonicalBuffer::IsValidUtf8(const std::string &value) noexcept {
  const auto *bytes = reinterpret_cast<const unsigned char *>(value.data());
  std::size_t index = 0U;
  while (index < value.size()) {
    const unsigned char first = bytes[index];
    if (first <= 0x7fU) {
      ++index;
      continue;
    }

    if (first >= 0xc2U && first <= 0xdfU) {
      if (index + 1U >= value.size() || bytes[index + 1U] < 0x80U || bytes[index + 1U] > 0xbfU) {
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

void CP2CanonicalBuffer::AppendUtf8(const std::string &value) {
  if (!IsValidUtf8(value)) {
    throw std::invalid_argument("CP2 canonical string is not valid UTF-8");
  }
  if (value.size() > std::numeric_limits<std::uint64_t>::max()) {
    throw std::length_error("CP2 canonical UTF-8 string exceeds u64 length");
  }
  AppendU64(static_cast<std::uint64_t>(value.size()));
  AppendRawBytes(value);
}

} // namespace ov_msckf
