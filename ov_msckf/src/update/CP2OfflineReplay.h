/*
 * SchurVIO-Lite CP2 detached offline replay entry point.
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#ifndef OV_MSCKF_CP2_OFFLINE_REPLAY_H
#define OV_MSCKF_CP2_OFFLINE_REPLAY_H

#include <cstdint>
#include <string>

namespace ov_msckf {

enum class CP2OfflineReplayStatus : std::uint8_t {
  kAccepted,
  kInvalidArguments,
  kInvalidPath,
  kInvalidArtifact,
  kReplayFailed,
  kOutputFailure,
  kAllocationFailure,
};

const char *
cp2_offline_replay_status_name(CP2OfflineReplayStatus status) noexcept;

struct CP2OfflineReplayPaths {
  std::string executable_path;
  std::string artifact_directory;
  std::string output_path;
};

struct CP2OfflineReplayResult {
  CP2OfflineReplayStatus status = CP2OfflineReplayStatus::kInvalidArguments;
  std::string detail;

  bool accepted() const noexcept {
    return status == CP2OfflineReplayStatus::kAccepted;
  }
};

/**
 * Parse the one exact mutually-exclusive command surface.
 *
 * argv[0], the artifact, and output must be normalized absolute paths. No
 * filesystem access is performed by this function.
 */
CP2OfflineReplayResult ParseCP2OfflineReplayArguments(
    int argc, const char *const *argv, CP2OfflineReplayPaths &paths) noexcept;

/**
 * Validate and replay one finalized CP2-C artifact without ROS or bag access.
 *
 * Identity normally comes from final provenance.json. During the runner's
 * pre-seal invocation only, provenance.json may be absent and the artifact
 * must instead contain transient replay_input.json with exact keys:
 * schema_version, record_type=cp2_offline_replay_input, checkpoint=CP2-C,
 * source_commit, source_tree, executable_sha256, executable_build_id,
 * strict_fp_verified, and resolved_parameters. The latter is an ordered array
 * of exact {sequence_index,canonical_path,canonical_sha256} records. Exactly
 * one identity file is accepted; replay_input.json is not a final artifact
 * member and must be removed before inventory/sealing.
 *
 * The output must already exist as an empty regular, nonsymlink, single-link
 * file. Success means a fully passing deterministic report was fsynced; failed
 * or structurally incomplete evidence never returns kAccepted.
 */
CP2OfflineReplayResult
RunCP2OfflineReplay(const CP2OfflineReplayPaths &paths) noexcept;

} // namespace ov_msckf

#endif // OV_MSCKF_CP2_OFFLINE_REPLAY_H
