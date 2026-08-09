/*
 * SchurVIO-Lite CP2 exact live-commit oracle and counter boundary.
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#ifndef OV_MSCKF_CP2_COMMIT_ORACLE_H
#define OV_MSCKF_CP2_COMMIT_ORACLE_H

#include "CP2CompositeState.h"

#include <Eigen/Core>

#include <cstdint>

namespace ov_msckf {

/**
 * Result availability for one complete phase-0/2/3 comparison.
 *
 * Canonical, finite, or detached-call disagreement is representable complete
 * evidence and therefore retains kComplete. Structural mismatch deliberately
 * suppresses every coefficient population, while arithmetic overflow makes
 * the entire result unavailable rather than exposing a partial count.
 */
enum class CP2CommitOracleStatus : std::uint8_t {
  kArithmeticOverflow,
  kInvalidPhase,
  kStructuralMismatch,
  kComplete,
};

const char *cp2_commit_oracle_status_name(CP2CommitOracleStatus status) noexcept;

/**
 * Exact schema-unit counters and verdicts for one committed updater call.
 *
 * The three mismatch counters count unequal IEEE-754 coefficient bits. The
 * covariance population is the complete ordered n-by-n matrix, including both
 * triangles. State/covariance row populations use the phase-0 semantic-block
 * count as expected and the phase-3 semantic-block count as seen.
 */
struct CP2CommitOracleResult {
  CP2CommitOracleStatus status = CP2CommitOracleStatus::kArithmeticOverflow;

  bool comparison_available = false;
  bool structure_equal = false;
  bool phase0_valid = false;
  bool phase2_valid = false;
  bool phase3_valid = false;
  bool complete_finite = false;
  bool phase0_phase2_immutable_equal = false;
  bool phase2_phase3_canonical_equal = false;
  bool complete_canonical_equal = false;
  bool type_update_calls_match = false;
  bool passed = false;

  std::uint64_t observed_type_update_calls = 0;
  std::uint64_t baseline_expected_type_update_calls = 0;
  std::uint64_t baseline_verified_nominal_fields = 0;
  std::uint64_t baseline_nominal_mismatches = 0;
  std::uint64_t baseline_covariance_mismatches = 0;
  std::uint64_t baseline_fej_mismatches = 0;

  std::uint64_t state_blocks_expected = 0;
  std::uint64_t state_blocks_seen = 0;
  std::uint64_t covariance_blocks_expected = 0;
  std::uint64_t covariance_blocks_seen = 0;
};

/** Checked conversions/arithmetic used by updater and artifact count paths. */
bool cp2_checked_eigen_index_to_u64(Eigen::Index value,
                                    std::uint64_t &output) noexcept;
bool cp2_checked_add_u64(std::uint64_t left, std::uint64_t right,
                         std::uint64_t &output) noexcept;
bool cp2_checked_multiply_u64(std::uint64_t left, std::uint64_t right,
                              std::uint64_t &output) noexcept;

class CP2CommitOracle {
public:
  /**
   * Compare a promoted phase 0, detached phase 2, and complete live phase 3.
   *
   * This function is allocation-free and nonthrowing. On arithmetic overflow
   * it returns a default, all-zero kArithmeticOverflow result. On structural
   * mismatch it retains exact expected calls and representable row populations
   * but zeros the verified-field and all coefficient-mismatch counters.
   */
  static CP2CommitOracleResult
  Compare(const CP2CompositeStateSnapshot &phase0,
          const CP2CompositeStateSnapshot &phase2,
          const CP2CompositeStateSnapshot &phase3,
          std::uint64_t observed_type_update_calls) noexcept;
};

} // namespace ov_msckf

#endif // OV_MSCKF_CP2_COMMIT_ORACLE_H
