/*
 * SchurVIO-Lite CP2 exact steady-clock timing primitive.
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#ifndef OV_MSCKF_CP2_TIMING_CLOCK_H
#define OV_MSCKF_CP2_TIMING_CLOCK_H

#include <cstdint>

namespace ov_msckf {

/** One checked std::chrono::steady_clock endpoint in integer nanoseconds. */
struct CP2SteadyClockEndpoint {
  std::uint64_t nanoseconds = 0U;
  bool valid = false;
};

/**
 * Convert one signed steady-clock tick count to the frozen u64 domain.
 *
 * This value-only surface exists so boundary and overflow behavior can be
 * tested without depending on the host clock. The production clock period is
 * compile-time constrained to one nanosecond, so no scaling or rounding is
 * permitted. Negative tick counts are rejected.
 */
bool cp2_steady_clock_from_ticks(std::int64_t ticks,
                                 std::uint64_t &output) noexcept;

/** Sample std::chrono::steady_clock as an exact checked u64 endpoint. */
CP2SteadyClockEndpoint cp2_steady_clock_now() noexcept;

/** Exact u64 subtraction, rejecting invalid or reverse-ordered endpoints. */
bool cp2_steady_clock_duration(
    const CP2SteadyClockEndpoint &start,
    const CP2SteadyClockEndpoint &end,
    std::uint64_t &duration_ns) noexcept;

/** Clock adapter used at the allocation-free live-commit boundary. */
class CP2SteadyClock final {
public:
  using time_point = CP2SteadyClockEndpoint;
  time_point now() noexcept { return cp2_steady_clock_now(); }
};

} // namespace ov_msckf

#endif // OV_MSCKF_CP2_TIMING_CLOCK_H
