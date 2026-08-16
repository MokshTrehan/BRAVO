/*
 * SPDX-License-Identifier: GPL-3.0-or-later
 * TurnSafe Session-1 passive callback installation boundary.
 */

#ifndef OV_MSCKF_TURNSAFE_CALLBACK_INSTALLER_H
#define OV_MSCKF_TURNSAFE_CALLBACK_INSTALLER_H

#include "update/TurnSafeDiagnostics.h"

#include <memory>

namespace ov_core {
class TrackKLT;
}

namespace ov_msckf {

class UpdaterMSCKF;

/** Fixed-size result for the diagnostic-only callback installation boundary. */
struct TurnSafeCallbackInstallationResult {
  bool success = false;
  bool tracker_callback_retained = false;
  bool updater_callback_retained = false;
  bool tracker_rollback_succeeded = true;
  bool updater_rollback_succeeded = true;
  TurnSafeCaptureDisableReason failure_reason =
      TurnSafeCaptureDisableReason::kNone;
};

/**
 * Install the passive frontend/updater observers as one fail-closed operation.
 * Callback construction, installation, and rollback are all exception-isolated.
 */
TurnSafeCallbackInstallationResult install_turnsafe_t0_callbacks(
    const std::shared_ptr<TurnSafeDiagnostics> &diagnostics,
    ov_core::TrackKLT *tracker, UpdaterMSCKF *updater) noexcept;

/**
 * Rebind only the passive frontend observer after an epoch replaces TrackKLT.
 *
 * The updater observer is already installed and its configuration becomes
 * immutable after the first update, so an epoch boundary must not attempt to
 * reinstall it. A failure disables diagnostics but never the estimator.
 */
bool reinstall_turnsafe_t0_frontend_callback(
    const std::shared_ptr<TurnSafeDiagnostics> &diagnostics,
    ov_core::TrackKLT *tracker) noexcept;

} // namespace ov_msckf

#endif // OV_MSCKF_TURNSAFE_CALLBACK_INSTALLER_H
