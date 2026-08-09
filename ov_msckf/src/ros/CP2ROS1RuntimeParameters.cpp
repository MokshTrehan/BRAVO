/*
 * SchurVIO-Lite CP2 ROS1 runtime-parameter capture.
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include "CP2ROS1RuntimeParameters.h"

#include "update/CP2Canonical.h"

#include <ros/param.h>

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <iomanip>
#include <limits>
#include <locale>
#include <set>
#include <sstream>
#include <utility>

namespace {

using ParameterMap = std::map<std::string, XmlRpc::XmlRpcValue>;

struct BytewiseLess {
  bool operator()(const std::string &left,
                  const std::string &right) const noexcept {
    return std::lexicographical_compare(
        left.begin(), left.end(), right.begin(), right.end(),
        [](char a, char b) {
          return static_cast<unsigned char>(a) <
                 static_cast<unsigned char>(b);
        });
  }
};

std::string json_string(const std::string &value) {
  std::ostringstream output;
  output.imbue(std::locale::classic());
  output << '"';
  static constexpr char kHex[] = "0123456789abcdef";
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

std::string json_double(double value) {
  std::ostringstream output;
  output.imbue(std::locale::classic());
  output << std::setprecision(std::numeric_limits<double>::max_digits10)
         << value;
  std::string text = output.str();
  if (text.find_first_of(".eE") == std::string::npos) {
    text += ".0";
  }
  return text;
}

void append_tag(ov_msckf::CP2CanonicalBuffer &output, char tag) {
  output.AppendRawBytes(&tag, 1U);
}

bool encode_value(const XmlRpc::XmlRpcValue &value,
                  ov_msckf::CP2CanonicalBuffer &canonical,
                  std::string &json,
                  ov_msckf::CP2ROS1ParameterStatus &status) {
  switch (value.getType()) {
  case XmlRpc::XmlRpcValue::TypeBoolean: {
    const bool converted = static_cast<bool>(value);
    append_tag(canonical, 'b');
    const std::uint8_t byte = converted ? UINT8_C(1) : UINT8_C(0);
    canonical.AppendRawBytes(&byte, 1U);
    json += converted ? "true" : "false";
    return true;
  }
  case XmlRpc::XmlRpcValue::TypeInt: {
    const std::int64_t converted =
        static_cast<std::int64_t>(static_cast<int>(value));
    append_tag(canonical, 'i');
    canonical.AppendI64(converted);
    json += std::to_string(converted);
    return true;
  }
  case XmlRpc::XmlRpcValue::TypeDouble: {
    const double converted = static_cast<double>(value);
    if (!std::isfinite(converted)) {
      status = ov_msckf::CP2ROS1ParameterStatus::kNonfiniteDouble;
      return false;
    }
    append_tag(canonical, 'f');
    canonical.AppendBinary64(converted);
    json += json_double(converted);
    return true;
  }
  case XmlRpc::XmlRpcValue::TypeString: {
    const std::string converted = static_cast<std::string>(value);
    if (converted.find('\0') != std::string::npos) {
      status = ov_msckf::CP2ROS1ParameterStatus::kEncodingFailure;
      return false;
    }
    append_tag(canonical, 's');
    canonical.AppendUtf8(converted);
    json += json_string(converted);
    return true;
  }
  case XmlRpc::XmlRpcValue::TypeArray: {
    const int count = value.size();
    if (count < 0) {
      status = ov_msckf::CP2ROS1ParameterStatus::kEncodingFailure;
      return false;
    }
    append_tag(canonical, 'l');
    canonical.AppendU64(static_cast<std::uint64_t>(count));
    json += '[';
    for (int index = 0; index < count; ++index) {
      if (index != 0) {
        json += ',';
      }
      if (!encode_value(value[index], canonical, json, status)) {
        return false;
      }
    }
    json += ']';
    return true;
  }
  case XmlRpc::XmlRpcValue::TypeStruct: {
    std::map<std::string, const XmlRpc::XmlRpcValue *, BytewiseLess> entries;
    for (auto iterator = value.begin(); iterator != value.end(); ++iterator) {
      if (iterator->first.find('\0') != std::string::npos ||
          !entries.emplace(iterator->first, &iterator->second).second) {
        status = ov_msckf::CP2ROS1ParameterStatus::kEncodingFailure;
        return false;
      }
    }
    append_tag(canonical, 'm');
    canonical.AppendU64(static_cast<std::uint64_t>(entries.size()));
    json += '{';
    std::size_t index = 0U;
    for (const auto &entry : entries) {
      if (index++ != 0U) {
        json += ',';
      }
      canonical.AppendUtf8(entry.first);
      json += json_string(entry.first);
      json += ':';
      if (!encode_value(*entry.second, canonical, json, status)) {
        return false;
      }
    }
    json += '}';
    return true;
  }
  case XmlRpc::XmlRpcValue::TypeInvalid:
  case XmlRpc::XmlRpcValue::TypeDateTime:
  case XmlRpc::XmlRpcValue::TypeBase64:
    status = ov_msckf::CP2ROS1ParameterStatus::kForbiddenType;
    return false;
  }
  status = ov_msckf::CP2ROS1ParameterStatus::kForbiddenType;
  return false;
}

bool cp2_parameter_name(const std::string &name) noexcept {
  static const std::string prefix = "/cp2_vio/";
  return name.size() > prefix.size() &&
         name.compare(0U, prefix.size(), prefix) == 0 &&
         name.find('\0') == std::string::npos;
}

} // namespace

const char *ov_msckf::cp2_ros1_parameter_status_name(
    CP2ROS1ParameterStatus status) noexcept {
  switch (status) {
  case CP2ROS1ParameterStatus::kAccepted: return "accepted";
  case CP2ROS1ParameterStatus::kParameterServerFailure:
    return "parameter_server_failure";
  case CP2ROS1ParameterStatus::kInvalidName: return "invalid_name";
  case CP2ROS1ParameterStatus::kForbiddenType: return "forbidden_type";
  case CP2ROS1ParameterStatus::kNonfiniteDouble: return "nonfinite_double";
  case CP2ROS1ParameterStatus::kEncodingFailure: return "encoding_failure";
  }
  return "invalid";
}

ov_msckf::CP2ROS1ParameterCapture
ov_msckf::EncodeCP2ROS1ResolvedParameters(
    const std::map<std::string, XmlRpc::XmlRpcValue> &parameters) noexcept {
  CP2ROS1ParameterCapture result;
  try {
    if (parameters.empty()) {
      result.status = CP2ROS1ParameterStatus::kEncodingFailure;
      return result;
    }
    std::map<std::string, const XmlRpc::XmlRpcValue *, BytewiseLess> ordered;
    for (const auto &entry : parameters) {
      if (!cp2_parameter_name(entry.first)) {
        result.status = CP2ROS1ParameterStatus::kInvalidName;
        return result;
      }
      ordered.emplace(entry.first, &entry.second);
    }

    CP2CanonicalBuffer canonical;
    static const char domain[] = "SchurVIO-CP2-ros-params-v1\0";
    canonical.AppendRawBytes(domain, sizeof(domain) - 1U);
    append_tag(canonical, 'm');
    canonical.AppendU64(static_cast<std::uint64_t>(ordered.size()));
    std::string json = "{";
    std::size_t index = 0U;
    for (const auto &entry : ordered) {
      if (index++ != 0U) {
        json += ',';
      }
      canonical.AppendUtf8(entry.first);
      json += json_string(entry.first);
      json += ':';
      if (!encode_value(*entry.second, canonical, json, result.status)) {
        return result;
      }
    }
    json += "}\n";
    result.raw_json = std::move(json);
    result.canonical_bytes = canonical.bytes();
    result.canonical_sha256 = canonical.Sha256Hex();
    result.status = CP2ROS1ParameterStatus::kAccepted;
    return result;
  } catch (...) {
    result = CP2ROS1ParameterCapture{};
    result.status = CP2ROS1ParameterStatus::kEncodingFailure;
    return result;
  }
}

ov_msckf::CP2ROS1ParameterCapture
ov_msckf::CaptureCP2ROS1ResolvedParameters() noexcept {
  try {
    std::vector<std::string> names;
    if (!ros::param::getParamNames(names)) {
      CP2ROS1ParameterCapture result;
      result.status = CP2ROS1ParameterStatus::kParameterServerFailure;
      return result;
    }
    ParameterMap parameters;
    static const std::string prefix = "/cp2_vio/";
    for (const std::string &name : names) {
      if (name.size() <= prefix.size() ||
          name.compare(0U, prefix.size(), prefix) != 0) {
        continue;
      }
      if (!cp2_parameter_name(name)) {
        CP2ROS1ParameterCapture result;
        result.status = CP2ROS1ParameterStatus::kInvalidName;
        return result;
      }
      XmlRpc::XmlRpcValue value;
      if (!ros::param::get(name, value) ||
          !parameters.emplace(name, std::move(value)).second) {
        CP2ROS1ParameterCapture result;
        result.status = CP2ROS1ParameterStatus::kParameterServerFailure;
        return result;
      }
    }
    return EncodeCP2ROS1ResolvedParameters(parameters);
  } catch (...) {
    CP2ROS1ParameterCapture result;
    result.status = CP2ROS1ParameterStatus::kParameterServerFailure;
    return result;
  }
}
