/*
 * SchurVIO-Lite CP2 ROS1 runtime-parameter encoding tests.
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include "ros/CP2ROS1RuntimeParameters.h"

#include "update/CP2Canonical.h"

#include <gtest/gtest.h>

#include <cstdint>
#include <cstring>
#include <limits>
#include <map>
#include <string>
#include <vector>

namespace {

void append_u64(std::vector<std::uint8_t> &bytes, std::uint64_t value) {
  for (unsigned index = 0U; index < 8U; ++index) {
    bytes.push_back(static_cast<std::uint8_t>(
        value >> (56U - 8U * index)));
  }
}

void append_i64(std::vector<std::uint8_t> &bytes, std::int64_t value) {
  append_u64(bytes, static_cast<std::uint64_t>(value));
}

void append_string(std::vector<std::uint8_t> &bytes,
                   const std::string &value) {
  append_u64(bytes, static_cast<std::uint64_t>(value.size()));
  bytes.insert(bytes.end(), value.begin(), value.end());
}

} // namespace

TEST(CP2ROS1RuntimeParameters,
     FrozenDomainTagsOrderingSignedIntegerAndNegativeZeroAreExact) {
  XmlRpc::XmlRpcValue values;
  values.setSize(2);
  values[0] = -2;
  values[1] = -0.0;
  std::map<std::string, XmlRpc::XmlRpcValue> parameters;
  parameters.emplace("/cp2_vio/z", values);
  parameters.emplace("/cp2_vio/a", true);

  const ov_msckf::CP2ROS1ParameterCapture encoded =
      ov_msckf::EncodeCP2ROS1ResolvedParameters(parameters);
  ASSERT_TRUE(encoded.accepted())
      << ov_msckf::cp2_ros1_parameter_status_name(encoded.status);

  const char domain[] = "SchurVIO-CP2-ros-params-v1\0";
  std::vector<std::uint8_t> expected(domain, domain + sizeof(domain) - 1U);
  expected.push_back('m');
  append_u64(expected, 2U);
  append_string(expected, "/cp2_vio/a");
  expected.push_back('b');
  expected.push_back(1U);
  append_string(expected, "/cp2_vio/z");
  expected.push_back('l');
  append_u64(expected, 2U);
  expected.push_back('i');
  append_i64(expected, -2);
  expected.push_back('f');
  append_u64(expected, UINT64_C(0x8000000000000000));

  EXPECT_EQ(encoded.canonical_bytes, expected);
  ov_msckf::CP2Sha256 digest;
  digest.Update(expected);
  EXPECT_EQ(encoded.canonical_sha256, digest.HexDigest());
  EXPECT_EQ(encoded.raw_json,
            "{\"/cp2_vio/a\":true,\"/cp2_vio/z\":[-2,-0.0]}\n");
}

TEST(CP2ROS1RuntimeParameters,
     NestedStructKeysUseUnsignedUtf8ByteOrderAndEscapeStrings) {
  XmlRpc::XmlRpcValue nested;
  nested["z"] = std::string("line\nquote\"");
  nested["\xC3\xA4"] = 7;
  std::map<std::string, XmlRpc::XmlRpcValue> parameters;
  parameters.emplace("/cp2_vio/nested", nested);
  const auto encoded =
      ov_msckf::EncodeCP2ROS1ResolvedParameters(parameters);
  ASSERT_TRUE(encoded.accepted());
  EXPECT_NE(encoded.raw_json.find("\"z\":\"line\\nquote\\\"\""),
            std::string::npos);
  const std::size_t z = encoded.raw_json.find("\"z\"");
  const std::size_t umlaut = encoded.raw_json.find("\"\xC3\xA4\"");
  ASSERT_NE(z, std::string::npos);
  ASSERT_NE(umlaut, std::string::npos);
  EXPECT_LT(z, umlaut);
}

TEST(CP2ROS1RuntimeParameters, BoolIntAndDoubleRemainDistinctTypedBytes) {
  std::map<std::string, XmlRpc::XmlRpcValue> boolean{{"/cp2_vio/x", true}};
  std::map<std::string, XmlRpc::XmlRpcValue> integer{{"/cp2_vio/x", 1}};
  std::map<std::string, XmlRpc::XmlRpcValue> floating{{"/cp2_vio/x", 1.0}};
  const auto a = ov_msckf::EncodeCP2ROS1ResolvedParameters(boolean);
  const auto b = ov_msckf::EncodeCP2ROS1ResolvedParameters(integer);
  const auto c = ov_msckf::EncodeCP2ROS1ResolvedParameters(floating);
  ASSERT_TRUE(a.accepted() && b.accepted() && c.accepted());
  EXPECT_NE(a.canonical_sha256, b.canonical_sha256);
  EXPECT_NE(a.canonical_sha256, c.canonical_sha256);
  EXPECT_NE(b.canonical_sha256, c.canonical_sha256);
  EXPECT_EQ(c.raw_json, "{\"/cp2_vio/x\":1.0}\n");
}

TEST(CP2ROS1RuntimeParameters, NonfiniteAndInvalidXmlRpcValuesFailClosed) {
  std::map<std::string, XmlRpc::XmlRpcValue> nonfinite{
      {"/cp2_vio/x", std::numeric_limits<double>::infinity()}};
  auto result = ov_msckf::EncodeCP2ROS1ResolvedParameters(nonfinite);
  EXPECT_EQ(result.status,
            ov_msckf::CP2ROS1ParameterStatus::kNonfiniteDouble);
  EXPECT_TRUE(result.canonical_bytes.empty());

  std::map<std::string, XmlRpc::XmlRpcValue> invalid{
      {"/cp2_vio/x", XmlRpc::XmlRpcValue()}};
  result = ov_msckf::EncodeCP2ROS1ResolvedParameters(invalid);
  EXPECT_EQ(result.status,
            ov_msckf::CP2ROS1ParameterStatus::kForbiddenType);
  EXPECT_TRUE(result.canonical_bytes.empty());
}

TEST(CP2ROS1RuntimeParameters, NamesAndStringsRejectUnsafeOrInvalidInputs) {
  std::map<std::string, XmlRpc::XmlRpcValue> relative{{"relative", 1}};
  EXPECT_EQ(ov_msckf::EncodeCP2ROS1ResolvedParameters(relative).status,
            ov_msckf::CP2ROS1ParameterStatus::kInvalidName);
  std::map<std::string, XmlRpc::XmlRpcValue> root{{"/cp2_vio/", 1}};
  EXPECT_EQ(ov_msckf::EncodeCP2ROS1ResolvedParameters(root).status,
            ov_msckf::CP2ROS1ParameterStatus::kInvalidName);
  std::map<std::string, XmlRpc::XmlRpcValue> nul{
      {"/cp2_vio/x", std::string("a\0b", 3U)}};
  EXPECT_EQ(ov_msckf::EncodeCP2ROS1ResolvedParameters(nul).status,
            ov_msckf::CP2ROS1ParameterStatus::kEncodingFailure);
}

TEST(CP2ROS1RuntimeParameters, EmptyPopulationCannotMasqueradeAsCapture) {
  const std::map<std::string, XmlRpc::XmlRpcValue> empty;
  const auto result = ov_msckf::EncodeCP2ROS1ResolvedParameters(empty);
  EXPECT_EQ(result.status,
            ov_msckf::CP2ROS1ParameterStatus::kEncodingFailure);
  EXPECT_TRUE(result.raw_json.empty());
  EXPECT_TRUE(result.canonical_bytes.empty());
  EXPECT_TRUE(result.canonical_sha256.empty());
}
