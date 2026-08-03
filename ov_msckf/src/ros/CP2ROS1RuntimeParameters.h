/*
 * SchurVIO-Lite CP2 ROS1 runtime-parameter capture.
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#ifndef OV_MSCKF_CP2_ROS1_RUNTIME_PARAMETERS_H
#define OV_MSCKF_CP2_ROS1_RUNTIME_PARAMETERS_H

#include <XmlRpcValue.h>

#include <map>
#include <string>
#include <vector>

namespace ov_msckf {

enum class CP2ROS1ParameterStatus {
  kAccepted,
  kParameterServerFailure,
  kInvalidName,
  kForbiddenType,
  kNonfiniteDouble,
  kEncodingFailure,
};

const char *cp2_ros1_parameter_status_name(
    CP2ROS1ParameterStatus status) noexcept;

struct CP2ROS1ParameterCapture {
  CP2ROS1ParameterStatus status =
      CP2ROS1ParameterStatus::kParameterServerFailure;
  std::string raw_json;
  std::vector<std::uint8_t> canonical_bytes;
  std::string canonical_sha256;

  bool accepted() const noexcept {
    return status == CP2ROS1ParameterStatus::kAccepted;
  }
};

/** Encode an already flattened map; exposed for artifact-free unit proof. */
CP2ROS1ParameterCapture EncodeCP2ROS1ResolvedParameters(
    const std::map<std::string, XmlRpc::XmlRpcValue> &parameters) noexcept;

/** Capture every live leaf whose absolute name starts with /cp2_vio/. */
CP2ROS1ParameterCapture CaptureCP2ROS1ResolvedParameters() noexcept;

} // namespace ov_msckf

#endif // OV_MSCKF_CP2_ROS1_RUNTIME_PARAMETERS_H
