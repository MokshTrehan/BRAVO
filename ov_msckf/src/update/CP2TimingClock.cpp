/*
 * SchurVIO-Lite CP2 exact steady-clock timing primitive.
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include "CP2TimingClock.h"

#include <chrono>
#include <limits>
#include <ratio>
#include <type_traits>

bool ov_msckf::cp2_steady_clock_from_ticks(
    std::int64_t ticks, std::uint64_t &output) noexcept {
  output = 0U;
  if (ticks < 0) {
    return false;
  }
  output = static_cast<std::uint64_t>(ticks);
  return true;
}

ov_msckf::CP2SteadyClockEndpoint
ov_msckf::cp2_steady_clock_now() noexcept {
  using Clock = std::chrono::steady_clock;
  using Rep = Clock::duration::rep;
  static_assert(Clock::is_steady,
                "CP2 timing requires a monotonic steady clock");
  static_assert(std::ratio_equal<Clock::period, std::nano>::value,
                "CP2 requires an exact nanosecond steady clock");
  static_assert(std::numeric_limits<Rep>::is_signed,
                "CP2 steady-clock ticks must be signed");
  static_assert(std::numeric_limits<Rep>::is_integer,
                "CP2 steady-clock ticks must be integral");
  static_assert(std::numeric_limits<Rep>::digits <=
                    std::numeric_limits<std::int64_t>::digits,
                "CP2 steady-clock ticks must fit signed 64-bit");

  // Keep the sample as the first runtime operation. In particular, the live
  // commit boundary permits no diagnostic initialization before this call.
  const Rep ticks = Clock::now().time_since_epoch().count();
  CP2SteadyClockEndpoint output;
  std::uint64_t nanoseconds = 0U;
  if (!cp2_steady_clock_from_ticks(static_cast<std::int64_t>(ticks),
                                   nanoseconds)) {
    return output;
  }
  output.nanoseconds = nanoseconds;
  output.valid = true;
  return output;
}

bool ov_msckf::cp2_steady_clock_duration(
    const CP2SteadyClockEndpoint &start,
    const CP2SteadyClockEndpoint &end,
    std::uint64_t &duration_ns) noexcept {
  duration_ns = 0U;
  if (!start.valid || !end.valid || end.nanoseconds < start.nanoseconds) {
    return false;
  }
  duration_ns = end.nanoseconds - start.nanoseconds;
  return true;
}
