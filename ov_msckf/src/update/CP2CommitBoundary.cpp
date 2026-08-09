/*
 * SchurVIO-Lite CP2 commit-boundary primitive.
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include "CP2CommitBoundary.h"

namespace ov_msckf {

CP2CommitBoundaryStatus CP2CommitBoundary::StatusFromPostcommitFill(
    CP2PostcommitFillStatus status) noexcept {
  return status == CP2PostcommitFillStatus::kSucceeded
             ? CP2CommitBoundaryStatus::kCommittedFillSucceeded
             : CP2CommitBoundaryStatus::kCommittedFillFailed;
}

} // namespace ov_msckf
