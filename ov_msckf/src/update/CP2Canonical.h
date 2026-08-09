/*
 * SchurVIO-Lite CP2 canonical serialization support.
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#ifndef OV_MSCKF_CP2_CANONICAL_H
#define OV_MSCKF_CP2_CANONICAL_H

#include <Eigen/Core>

#include <array>
#include <cstddef>
#include <cstdint>
#include <stdexcept>
#include <string>
#include <type_traits>
#include <vector>

namespace ov_msckf {

/// Dependency-free, incremental SHA-256 with repeatable non-mutating digests.
class CP2Sha256 {
public:
  CP2Sha256() noexcept;

  /// Append exactly @p size bytes. A null pointer is valid only for size zero.
  void Update(const void *data, std::size_t size);
  void Update(const std::vector<std::uint8_t> &bytes);
  void Update(const std::string &bytes);

  /// Return the digest without finalizing or otherwise changing this instance.
  std::array<std::uint8_t, 32> Digest() const noexcept;
  std::string HexDigest() const;

private:
  void Transform(const std::uint8_t block[64]) noexcept;
  std::array<std::uint8_t, 32> FinalizeCopy() noexcept;

  std::array<std::uint32_t, 8> state_;
  std::array<std::uint8_t, 64> buffer_;
  std::size_t buffer_size_;
  std::uint64_t total_bytes_;
};

/**
 * Owning canonical byte buffer for the frozen CP2 primitive encodings.
 *
 * The buffer has no estimator references and performs no floating-point
 * arithmetic. Binary64 values are copied bit-for-bit before big-endian output,
 * preserving signed zero and every other IEEE-754 representation.
 */
class CP2CanonicalBuffer {
public:
  CP2CanonicalBuffer() = default;

  void AppendRawBytes(const void *data, std::size_t size);
  void AppendRawBytes(const std::vector<std::uint8_t> &bytes);
  void AppendRawBytes(const std::string &bytes);

  void AppendU64(std::uint64_t value);
  void AppendI64(std::int64_t value);
  void AppendBinary64(double value);

  /// Append a validated UTF-8 string as u64 byte length followed by its bytes.
  void AppendUtf8(const std::string &value);

  /// Append (rows, columns), then binary64 coefficients in logical row-major order.
  template <typename Derived> void AppendMatrix(const Eigen::MatrixBase<Derived> &value) {
    static_assert(std::is_same<typename Derived::Scalar, double>::value,
                  "CP2 canonical matrices must contain binary64 scalars");
    if (value.rows() < 0 || value.cols() < 0) {
      throw std::invalid_argument("CP2 canonical matrix dimensions must be nonnegative");
    }
    AppendU64(static_cast<std::uint64_t>(value.rows()));
    AppendU64(static_cast<std::uint64_t>(value.cols()));
    for (Eigen::Index row = 0; row < value.rows(); ++row) {
      for (Eigen::Index column = 0; column < value.cols(); ++column) {
        const double coefficient = value.derived().coeff(row, column);
        AppendBinary64(coefficient);
      }
    }
  }

  /// Append a column vector using the contract's (rows, 1) matrix encoding.
  template <typename Derived> void AppendVector(const Eigen::MatrixBase<Derived> &value) {
    static_assert(std::is_same<typename Derived::Scalar, double>::value,
                  "CP2 canonical vectors must contain binary64 scalars");
    if (value.rows() < 0 || value.cols() != 1) {
      throw std::invalid_argument("CP2 canonical vector must have exactly one column");
    }
    AppendU64(static_cast<std::uint64_t>(value.rows()));
    AppendU64(1U);
    for (Eigen::Index row = 0; row < value.rows(); ++row) {
      const double coefficient = value.derived().coeff(row, 0);
      AppendBinary64(coefficient);
    }
  }

  const std::vector<std::uint8_t> &bytes() const noexcept { return bytes_; }
  std::size_t size() const noexcept { return bytes_.size(); }
  std::string Sha256Hex() const { return sha256_.HexDigest(); }

private:
  static bool IsValidUtf8(const std::string &value) noexcept;

  std::vector<std::uint8_t> bytes_;
  CP2Sha256 sha256_;
};

} // namespace ov_msckf

#endif // OV_MSCKF_CP2_CANONICAL_H
