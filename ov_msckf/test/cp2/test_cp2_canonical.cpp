/*
 * SchurVIO-Lite CP2 canonical serialization tests.
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include "update/CP2Canonical.h"

#include <gtest/gtest.h>

#include <Eigen/Core>

#include <algorithm>
#include <cstdint>
#include <stdexcept>
#include <string>
#include <vector>

namespace {

std::string BytesToHex(const std::vector<std::uint8_t> &bytes) {
  static constexpr char kHexDigits[] = "0123456789abcdef";
  std::string result(2U * bytes.size(), '0');
  for (std::size_t index = 0; index < bytes.size(); ++index) {
    result[2U * index] = kHexDigits[bytes[index] >> 4U];
    result[2U * index + 1U] = kHexDigits[bytes[index] & 0x0fU];
  }
  return result;
}

TEST(CP2CanonicalSha256, MatchesPublishedVectorsUnderIncrementalChunking) {
  ov_msckf::CP2Sha256 empty;
  EXPECT_EQ(empty.HexDigest(), "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855");
  EXPECT_EQ(empty.HexDigest(), "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855");

  ov_msckf::CP2Sha256 incremental;
  incremental.Update("a", 1U);
  const std::string digest_after_a = incremental.HexDigest();
  EXPECT_EQ(digest_after_a, "ca978112ca1bbdcafac231b39a23dc4da786eff8147c4e72b9807785afee48bb");
  incremental.Update("b", 1U);
  incremental.Update("c", 1U);
  EXPECT_EQ(incremental.HexDigest(), "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");

  const std::string long_vector = "abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq";
  ov_msckf::CP2Sha256 chunked;
  const std::size_t chunk_sizes[] = {1U, 7U, 2U, 13U, 5U, 19U, 3U, 11U};
  std::size_t offset = 0U;
  std::size_t chunk_index = 0U;
  while (offset < long_vector.size()) {
    const std::size_t size = std::min(chunk_sizes[chunk_index % 8U], long_vector.size() - offset);
    chunked.Update(long_vector.data() + offset, size);
    offset += size;
    ++chunk_index;
  }
  EXPECT_EQ(chunked.HexDigest(), "248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1");

  ov_msckf::CP2Sha256 million_a;
  const std::string thousand_a(1000U, 'a');
  for (std::size_t index = 0; index < 1000U; ++index) {
    million_a.Update(thousand_a);
  }
  EXPECT_EQ(million_a.HexDigest(), "cdc76e5c9914fb9281a1c7e284d73e67f1809a48a497200e046d39ccc7112cd0");

  EXPECT_THROW(chunked.Update(nullptr, 1U), std::invalid_argument);
  EXPECT_NO_THROW(chunked.Update(nullptr, 0U));
}

TEST(CP2CanonicalBytes, IntegerBinary64AndUtf8EncodingIsExact) {
  ov_msckf::CP2CanonicalBuffer canonical;
  canonical.AppendU64(UINT64_C(0x0123456789abcdef));
  canonical.AppendI64(-2);
  canonical.AppendBinary64(0.0);
  canonical.AppendBinary64(-0.0);
  canonical.AppendUtf8(std::string("A\xc3\xa9", 3U));

  EXPECT_EQ(BytesToHex(canonical.bytes()),
            "0123456789abcdef"
            "fffffffffffffffe"
            "0000000000000000"
            "8000000000000000"
            "0000000000000003"
            "41c3a9");
  EXPECT_EQ(canonical.size(), 43U);
  EXPECT_EQ(canonical.Sha256Hex(), "11776051afe7355b64cafd62cd060fa03e7f7335962c9fb5b2a43f53851b3f2a");

  ov_msckf::CP2CanonicalBuffer minimum;
  minimum.AppendI64(INT64_MIN);
  EXPECT_EQ(BytesToHex(minimum.bytes()), "8000000000000000");
}

TEST(CP2CanonicalBytes, MatrixAndVectorUseLogicalRowMajorBinary64Order) {
  Eigen::MatrixXd column_major(2, 3);
  column_major << 1.0, -0.0, 2.5, -2.0, 0.0, -4.25;
  Eigen::Matrix<double, Eigen::Dynamic, Eigen::Dynamic, Eigen::RowMajor> row_major = column_major;

  ov_msckf::CP2CanonicalBuffer from_column_major;
  from_column_major.AppendMatrix(column_major);
  ov_msckf::CP2CanonicalBuffer from_row_major;
  from_row_major.AppendMatrix(row_major);
  EXPECT_EQ(from_column_major.bytes(), from_row_major.bytes());
  EXPECT_EQ(BytesToHex(from_column_major.bytes()),
            "0000000000000002"
            "0000000000000003"
            "3ff0000000000000"
            "8000000000000000"
            "4004000000000000"
            "c000000000000000"
            "0000000000000000"
            "c011000000000000");

  Eigen::Vector3d vector;
  vector << -0.0, 1.0, -2.5;
  ov_msckf::CP2CanonicalBuffer vector_bytes;
  vector_bytes.AppendVector(vector);
  EXPECT_EQ(BytesToHex(vector_bytes.bytes()),
            "0000000000000003"
            "0000000000000001"
            "8000000000000000"
            "3ff0000000000000"
            "c004000000000000");

  Eigen::RowVector2d row_vector;
  row_vector << 1.0, 2.0;
  EXPECT_THROW(vector_bytes.AppendVector(row_vector), std::invalid_argument);
}

TEST(CP2CanonicalBytes, Utf8ValidationRejectsMalformedSequencesWithoutAppending) {
  const std::vector<std::string> malformed{
      std::string("\x80", 1U),       std::string("\xc0\x80", 2U), std::string("\xe0\x80\x80", 3U),
      std::string("\xed\xa0\x80", 3U), std::string("\xf4\x90\x80\x80", 4U), std::string("\xe2\x82", 2U),
  };
  for (const std::string &value : malformed) {
    ov_msckf::CP2CanonicalBuffer canonical;
    EXPECT_THROW(canonical.AppendUtf8(value), std::invalid_argument);
    EXPECT_TRUE(canonical.bytes().empty());
    EXPECT_EQ(canonical.Sha256Hex(), "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855");
  }

  ov_msckf::CP2CanonicalBuffer valid;
  EXPECT_NO_THROW(valid.AppendUtf8(std::string("\0\xf0\x9f\x98\x80", 5U)));
  EXPECT_EQ(BytesToHex(valid.bytes()), "000000000000000500f09f9880");
}

TEST(CP2CanonicalBytes, SelfAppendStagesAliasedStorageBeforeGrowth) {
  ov_msckf::CP2CanonicalBuffer canonical;
  canonical.AppendRawBytes(std::string("abc"));
  canonical.AppendRawBytes(canonical.bytes());

  EXPECT_EQ(std::string(canonical.bytes().begin(), canonical.bytes().end()), "abcabc");
  EXPECT_EQ(canonical.Sha256Hex(), "bbb59da3af939f7af5f360f2ceb80a496e3bae1cd87dde426db0ae40677e1c2c");

  ov_msckf::CP2CanonicalBuffer subrange;
  subrange.AppendRawBytes(std::string("abc"));
  subrange.AppendRawBytes(subrange.bytes().data() + 1, 2U);
  EXPECT_EQ(std::string(subrange.bytes().begin(), subrange.bytes().end()), "abcbc");
}

} // namespace
