/*
 * OpenVINS: An Open Platform for Visual-Inertial Research
 * Copyright (C) 2018-2023 Patrick Geneva
 * Copyright (C) 2018-2023 Guoquan Huang
 * Copyright (C) 2018-2023 OpenVINS Contributors
 * Copyright (C) 2018-2019 Kevin Eckenhoff
 * Copyright (C) 2026 Moksh Trehan
 * Modified in 2026 by Moksh Trehan for SchurVIO-Lite CP2.
 *
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation, either version 3 of the License, or
 * (at your option) any later version.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.
 *
 * You should have received a copy of the GNU General Public License
 * along with this program.  If not, see <https://www.gnu.org/licenses/>.
 */

#ifndef OV_MSCKF_UPDATER_OPTIONS_H
#define OV_MSCKF_UPDATER_OPTIONS_H

#include <cstdlib>
#include <string>

#include "utils/colors.h"
#include "utils/print.h"

namespace ov_msckf {

/**
 * @brief Struct which stores general updater options
 */
struct UpdaterOptions {

  /// Supported transient-landmark elimination implementations.
  enum class LandmarkElimination { NULLSPACE, SCHUR };

  /// What chi-squared multipler we should apply
  double chi2_multipler = 5;

  /// Noise sigma for our raw pixel measurements
  double sigma_pix = 1;

  /// Covariance for our raw pixel measurements
  double sigma_pix_sq = 1;

  /// How transient MSCKF landmarks should be eliminated.
  LandmarkElimination landmark_elimination = LandmarkElimination::NULLSPACE;

  /// Fixed maximum number of visual update passes (one or two).
  int max_visual_passes = 1;

  /// Research-only raw camera-system capture; default-off and one-pass only.
  bool capture_conditioning_systems = false;

  /// Absolute, create-new destination for the conditioning capture.
  std::string conditioning_capture_path;

  /// Main estimator configuration whose exact bytes are hashed into the file.
  std::string conditioning_capture_config_path;

  /// Publication-study update-envelope capture (schema 2); default off.
  bool capture_update_envelopes_v2 = false;

  /// Absolute, create-new destination for the schema-2 capture.
  std::string update_envelope_capture_path;

  /// Stable capture campaign identity written into the schema-2 header.
  std::string update_envelope_run_id;

  /// Stable dataset-sequence identity written into the schema-2 header.
  std::string update_envelope_sequence_id;

  /// Main estimator configuration whose exact bytes are hashed into schema 2.
  std::string update_envelope_capture_config_path;

  /// Whether a landmark-elimination enum is one of the two public modes.
  static bool landmark_elimination_is_supported(LandmarkElimination mode) noexcept {
    return mode == LandmarkElimination::NULLSPACE || mode == LandmarkElimination::SCHUR;
  }

  /// Whether the configured fixed visual-pass count is public and supported.
  static bool max_visual_passes_is_supported(int count) noexcept { return count == 1 || count == 2; }

  /// Whether the reducer supports the configured fixed pass count.
  static bool visual_pass_combination_is_supported(int count, LandmarkElimination mode) noexcept {
    return landmark_elimination_is_supported(mode) && max_visual_passes_is_supported(count) &&
           (count == 1 || mode == LandmarkElimination::SCHUR);
  }

  /// Return the configuration spelling for a landmark-elimination mode.
  static std::string landmark_elimination_as_string(LandmarkElimination mode) {
    if (mode == LandmarkElimination::NULLSPACE) {
      return "nullspace";
    }
    if (mode == LandmarkElimination::SCHUR) {
      return "schur";
    }
    return "unknown";
  }

  /**
   * @brief Parse an exact public configuration spelling without changing the
   * destination on failure.
   *
   * Keeping this classification separate from the CLI/API exit policy gives
   * the startup tests a non-destructive way to prove that an invalid spelling
   * cannot silently select either reducer.
   */
  static bool try_landmark_elimination_from_string(const std::string &mode, LandmarkElimination &parsed_mode) noexcept {
    if (mode == "nullspace") {
      parsed_mode = LandmarkElimination::NULLSPACE;
      return true;
    }
    if (mode == "schur") {
      parsed_mode = LandmarkElimination::SCHUR;
      return true;
    }
    return false;
  }

  /// Parse the exact public configuration spelling or terminate startup.
  static LandmarkElimination landmark_elimination_from_string_or_exit(const std::string &mode) {
    LandmarkElimination parsed_mode = LandmarkElimination::NULLSPACE;
    if (try_landmark_elimination_from_string(mode, parsed_mode)) {
      return parsed_mode;
    }
    PRINT_ERROR(RED "invalid MSCKF landmark elimination mode: %s\n" RESET, mode.c_str());
    PRINT_ERROR(RED "please select a valid mode: nullspace, schur\n" RESET);
    std::exit(EXIT_FAILURE);
  }

  /// Nice print function of what parameters we have loaded
  void print() {
    PRINT_DEBUG("    - chi2_multipler: %.1f\n", chi2_multipler);
    PRINT_DEBUG("    - sigma_pix: %.2f\n", sigma_pix);
    PRINT_DEBUG("    - capture_conditioning_systems: %d\n",
                (int)capture_conditioning_systems);
    if (capture_conditioning_systems) {
      PRINT_DEBUG("    - conditioning_capture_path: %s\n",
                  conditioning_capture_path.c_str());
    }
    PRINT_DEBUG("    - capture_update_envelopes_v2: %d\n",
                (int)capture_update_envelopes_v2);
    if (capture_update_envelopes_v2) {
      PRINT_DEBUG("    - update_envelope_capture_path: %s\n",
                  update_envelope_capture_path.c_str());
      PRINT_DEBUG("    - update_envelope_run_id: %s\n",
                  update_envelope_run_id.c_str());
      PRINT_DEBUG("    - update_envelope_sequence_id: %s\n",
                  update_envelope_sequence_id.c_str());
    }
  }
};

} // namespace ov_msckf

#endif // OV_MSCKF_UPDATER_OPTIONS_H
