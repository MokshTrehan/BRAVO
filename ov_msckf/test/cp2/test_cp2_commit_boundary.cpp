/*
 * SchurVIO-Lite CP2 commit-boundary protecting tests.
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include "update/CP2CommitBoundary.h"

#include <gtest/gtest.h>

#include <array>
#include <cstddef>
#include <cstdint>
#include <type_traits>

namespace {

enum class Observation {
  kProof,
  kCommit,
  kClock,
  kFill,
};

struct Trace {
  std::array<Observation, 4> observations{};
  std::size_t count = 0U;
  bool overflow = false;

  void observe(Observation observation) noexcept {
    if (count < observations.size()) {
      observations[count] = observation;
      ++count;
      return;
    }
    overflow = true;
  }
};

struct FinalProof {
  Trace *trace = nullptr;
  bool accepted = false;

  bool operator()() {
    trace->observe(Observation::kProof);
    return accepted;
  }
};

struct CommitFailure final {};

struct SoleCommit {
  Trace *trace = nullptr;
  bool throw_on_call = false;

  void operator()() {
    trace->observe(Observation::kCommit);
    if (throw_on_call) {
      throw CommitFailure();
    }
  }
};

struct FakeClock {
  using time_point = std::uint64_t;

  Trace *trace = nullptr;
  time_point value = 0U;

  time_point now() noexcept {
    trace->observe(Observation::kClock);
    return value;
  }
};

struct PostcommitFill {
  Trace *trace = nullptr;
  ov_msckf::CP2PostcommitFillStatus result =
      ov_msckf::CP2PostcommitFillStatus::kSucceeded;

  ov_msckf::CP2PostcommitFillStatus operator()() noexcept {
    trace->observe(Observation::kFill);
    return result;
  }
};

using Output = ov_msckf::CP2CommitBoundaryOutput<FakeClock::time_point>;

static_assert(noexcept(std::declval<FakeClock &>().now()),
              "fake clock must model the postcommit noexcept contract");
static_assert(noexcept(std::declval<PostcommitFill &>()()),
              "fake fill must model the postcommit noexcept contract");
static_assert(std::is_trivially_copyable<FakeClock::time_point>::value,
              "fake endpoint must model the bounded publication contract");

void expect_trace(const Trace &trace,
                  const std::array<Observation, 4> &expected,
                  std::size_t expected_count) {
  ASSERT_FALSE(trace.overflow);
  ASSERT_EQ(trace.count, expected_count);
  for (std::size_t index = 0U; index < expected_count; ++index) {
    EXPECT_EQ(trace.observations[index], expected[index]);
  }
}

TEST(CP2CommitBoundary, AcceptedPathHasExactProofCommitClockFillOrder) {
  Trace trace;
  FinalProof proof{&trace, true};
  SoleCommit commit{&trace, false};
  FakeClock clock{&trace, 0x123456789abcdef0ULL};
  PostcommitFill fill{&trace,
                      ov_msckf::CP2PostcommitFillStatus::kSucceeded};
  Output output;

  const ov_msckf::CP2CommitBoundaryStatus status =
      ov_msckf::CP2CommitBoundary::run(proof, commit, clock, fill, output);

  expect_trace(trace,
               {{Observation::kProof, Observation::kCommit,
                 Observation::kClock, Observation::kFill}},
               4U);
  EXPECT_EQ(status,
            ov_msckf::CP2CommitBoundaryStatus::kCommittedFillSucceeded);
  EXPECT_EQ(output.status, status);
  EXPECT_TRUE(output.endpoint_valid);
  EXPECT_EQ(output.endpoint, clock.value);
}

TEST(CP2CommitBoundary, RejectedProofSuppressesEveryPostproofOperation) {
  Trace trace;
  FinalProof proof{&trace, false};
  SoleCommit commit{&trace, false};
  FakeClock clock{&trace, 101U};
  PostcommitFill fill{&trace,
                      ov_msckf::CP2PostcommitFillStatus::kSucceeded};
  Output output;
  output.status =
      ov_msckf::CP2CommitBoundaryStatus::kCommittedFillSucceeded;
  output.endpoint_valid = true;
  output.endpoint = 77U;

  const ov_msckf::CP2CommitBoundaryStatus status =
      ov_msckf::CP2CommitBoundary::run(proof, commit, clock, fill, output);

  expect_trace(trace,
               {{Observation::kProof, Observation::kProof,
                 Observation::kProof, Observation::kProof}},
               1U);
  EXPECT_EQ(status, ov_msckf::CP2CommitBoundaryStatus::kProofRejected);
  EXPECT_EQ(output.status, status);
  EXPECT_FALSE(output.endpoint_valid);
  EXPECT_EQ(output.endpoint, 77U);
}

TEST(CP2CommitBoundary, ThrowingCommitPropagatesBeforeClockAndPreservesOutput) {
  Trace trace;
  FinalProof proof{&trace, true};
  SoleCommit commit{&trace, true};
  FakeClock clock{&trace, 202U};
  PostcommitFill fill{&trace,
                      ov_msckf::CP2PostcommitFillStatus::kSucceeded};
  Output output;
  output.status = ov_msckf::CP2CommitBoundaryStatus::kCommittedFillFailed;
  output.endpoint_valid = true;
  output.endpoint = 88U;

  EXPECT_THROW(
      ov_msckf::CP2CommitBoundary::run(proof, commit, clock, fill, output),
      CommitFailure);

  expect_trace(trace,
               {{Observation::kProof, Observation::kCommit,
                 Observation::kProof, Observation::kProof}},
               2U);
  EXPECT_EQ(output.status,
            ov_msckf::CP2CommitBoundaryStatus::kCommittedFillFailed);
  EXPECT_TRUE(output.endpoint_valid);
  EXPECT_EQ(output.endpoint, 88U);
}

TEST(CP2CommitBoundary, FailedFillRetainsCommittedStatusAndExactEndpoint) {
  Trace trace;
  FinalProof proof{&trace, true};
  SoleCommit commit{&trace, false};
  FakeClock clock{&trace, 303U};
  PostcommitFill fill{&trace, ov_msckf::CP2PostcommitFillStatus::kFailed};
  Output output;

  const ov_msckf::CP2CommitBoundaryStatus status =
      ov_msckf::CP2CommitBoundary::run(proof, commit, clock, fill, output);

  expect_trace(trace,
               {{Observation::kProof, Observation::kCommit,
                 Observation::kClock, Observation::kFill}},
               4U);
  EXPECT_EQ(status,
            ov_msckf::CP2CommitBoundaryStatus::kCommittedFillFailed);
  EXPECT_EQ(output.status, status);
  EXPECT_TRUE(output.endpoint_valid);
  EXPECT_EQ(output.endpoint, 303U);
}

} // namespace
