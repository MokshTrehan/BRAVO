/*
 * SchurVIO-Lite CP2 commit-boundary primitive.
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#ifndef OV_MSCKF_CP2_COMMIT_BOUNDARY_H
#define OV_MSCKF_CP2_COMMIT_BOUNDARY_H

#include <type_traits>
#include <utility>

namespace ov_msckf {

/// Result of the mandatory allocation-free postcommit fill.
enum class CP2PostcommitFillStatus {
  kSucceeded,
  kFailed,
};

/// Terminal result of one commit-boundary execution.
enum class CP2CommitBoundaryStatus {
  kProofRejected,
  kCommittedFillSucceeded,
  kCommittedFillFailed,
};

/**
 * @brief Caller-owned, preallocated output from the commit boundary.
 *
 * endpoint is meaningful exactly when endpoint_valid is true. A rejected proof
 * leaves endpoint unchanged and clears endpoint_valid. If commit throws, the
 * exception propagates and the complete output remains unchanged; the caller
 * must treat that case as fatal because the primitive cannot prove whether a
 * throwing production commit partially mutated live state.
 */
template <typename Endpoint> struct CP2CommitBoundaryOutput {
  CP2CommitBoundaryStatus status = CP2CommitBoundaryStatus::kProofRejected;
  bool endpoint_valid = false;
  Endpoint endpoint{};
};

/**
 * @brief Execute the frozen CP2 proof/commit/endpoint/fill boundary.
 *
 * The accepted-path statements below are intentionally adjacent. Do not add
 * logging, counters, tracing, callbacks, state reads, or other operations
 * between final_precommit_proof, sole_commit, clock.now(), and postcommit_fill.
 * Observable ordering belongs inside the injected callables.
 *
 * postcommit_fill must be allocation-free in addition to being noexcept. C++14
 * cannot express allocation freedom as a type trait, so the caller and the
 * protecting allocation-failure test own that part of the contract. Endpoint
 * is restricted to a trivially copyable value so publishing it after commit
 * cannot invoke user code or allocate.
 */
class CP2CommitBoundary final {
public:
  template <typename FinalPrecommitProof, typename SoleCommit, typename Clock,
            typename PostcommitFill, typename Endpoint>
  static CP2CommitBoundaryStatus
  run(FinalPrecommitProof &final_precommit_proof, SoleCommit &sole_commit,
      Clock &clock, PostcommitFill &postcommit_fill,
      CP2CommitBoundaryOutput<Endpoint> &output) {
    using ProofResult = decltype(std::declval<FinalPrecommitProof &>()());
    using CommitResult = decltype(std::declval<SoleCommit &>()());
    using ClockResult = decltype(std::declval<Clock &>().now());
    using FillResult = decltype(std::declval<PostcommitFill &>()());

    static_assert(std::is_same<typename std::decay<ProofResult>::type,
                               bool>::value,
                  "CP2 final precommit proof must return bool");
    static_assert(std::is_same<CommitResult, void>::value,
                  "CP2 sole commit must return void");
    static_assert(std::is_same<typename std::decay<ClockResult>::type,
                               Endpoint>::value,
                  "CP2 clock endpoint type must match the output type");
    static_assert(noexcept(std::declval<Clock &>().now()),
                  "CP2 postcommit endpoint sampling must be noexcept");
    static_assert(std::is_same<typename std::decay<FillResult>::type,
                               CP2PostcommitFillStatus>::value,
                  "CP2 postcommit fill must return CP2PostcommitFillStatus");
    static_assert(noexcept(std::declval<PostcommitFill &>()()),
                  "CP2 postcommit fill must be noexcept");
    static_assert(std::is_trivially_copyable<Endpoint>::value,
                  "CP2 endpoint must be trivially copyable");
    static_assert(std::is_nothrow_constructible<Endpoint, ClockResult>::value,
                  "CP2 endpoint capture must be nonthrowing");
    static_assert(
        std::is_nothrow_assignable<Endpoint &, const Endpoint &>::value,
        "CP2 endpoint publication must be nonthrowing");
    static_assert(std::is_nothrow_destructible<Endpoint>::value,
                  "CP2 endpoint destruction must be nonthrowing");

    if (!final_precommit_proof()) {
      output.status = CP2CommitBoundaryStatus::kProofRejected;
      output.endpoint_valid = false;
      return output.status;
    }
    sole_commit();
    const Endpoint endpoint = clock.now();
    const CP2PostcommitFillStatus fill_status = postcommit_fill();

    output.endpoint = endpoint;
    output.endpoint_valid = true;
    output.status =
        fill_status == CP2PostcommitFillStatus::kSucceeded
            ? CP2CommitBoundaryStatus::kCommittedFillSucceeded
            : CP2CommitBoundaryStatus::kCommittedFillFailed;
    return output.status;
  }

  CP2CommitBoundary() = delete;
};

} // namespace ov_msckf

#endif // OV_MSCKF_CP2_COMMIT_BOUNDARY_H
