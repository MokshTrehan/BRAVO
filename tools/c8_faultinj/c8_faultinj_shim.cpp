/*
 * C8 fault-injection shim (DESKTOP_EVIDENCE_SESSION v2, Item 1 / ledger F4).
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * LD_PRELOAD interposition on the FROZEN estimator binary + library. The
 * estimator source is not modified; every injector lives here, at the
 * boundary of one camera-callback update envelope
 * (UpdaterMSCKF::update(state, features)) or at the factor-production seam
 * (UpdaterHelper::get_feature_jacobian_full) / the proposal handoff seam
 * (StateHelper::CommitPrecomputedUpdate) inside that envelope.
 *
 * Every hooked function forwards to the real symbol via dlsym(RTLD_NEXT).
 * This library deliberately has NO link-time dependency on the estimator
 * libraries so that it is inert when preloaded into unrelated processes
 * (roscore, python drivers): all estimator symbols are resolved lazily and
 * only from within processes that actually call the hooked functions.
 *
 * Environment (read once, at first hooked call):
 *   C8_MODE      capture | cycle | single          (default capture)
 *   C8_CLASS     1..8 for mode=single             (fault class, see below)
 *   C8_PERIOD    N   inject when invocation % N == C8_PHASE (single mode)
 *   C8_PHASE     P   (default 0)
 *   C8_CYCLE_STRIDE S  cycle mode: every S-th ELIGIBLE invocation (one with
 *                at least one input feature) injects; classes cycle over
 *                {1,2,3,4,5,6,8}                  (default 8)
 *   C8_LOG       path of the JSONL record file (required for any mode)
 *   C8_T0_STAGE  integer TurnSafeDiagnosticFaultStage; with C8_T0_KIND
 *                (1=bad_alloc 2=std::exception 3=unknown) and C8_T0_AT
 *                (invocation index) arms the class-7 diagnostics fault
 *
 * Fault classes (EXECUTION_PLAN E7 / DESKTOP_EVIDENCE_SESSION Item 1):
 *   1 invalid factor dimensions        (residual rows != H_f rows)
 *   2 nonfinite factor values          (residual(0) = NaN)
 *   3 rank failure                     (H_f column 2 := column 1)
 *   4 innovation-factorization failure (prior snapshot covariance := -1e6 P
 *                                      at the preview/proposal boundary)
 *   5 negative posterior diagonal      (live prior IMU/rest cross-covariance x1e6)
 *   6 prior changed between preview and commit
 *                                      (IMU nominal perturbed after preview)
 *   7 logging/diagnostics failure      (TurnSafe T0 stream fault, separate)
 *   8 feature/factor failure after proposal construction
 *                                      (dx(0) := NaN at the commit handoff)
 */
#define _GNU_SOURCE 1
#include <dlfcn.h>
#include <openssl/evp.h>

#include <atomic>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <limits>
#include <memory>
#include <mutex>
#include <string>
#include <vector>

#include "feat/Feature.h"
#include "state/State.h"
#include "state/StateHelper.h"
#include "types/Type.h"
#include "update/TurnSafeDiagnostics.h"
#include "update/UpdaterHelper.h"
#include "update/UpdaterMSCKF.h"
#include "update/UpdaterMSCKFPreview.h"

// State::_Cov / State::_variables are private (friends: StateHelper,
// UpdaterMSCKFPreview, CP2CompositeStateAdapter). The mutation oracle needs
// read access and the prior-corruption classes need write access. Explicit
// template instantiation may legally name private members; no layout change.
namespace c8_access {
template <typename Tag> struct result { typedef typename Tag::type type; static type ptr; };
template <typename Tag> typename result<Tag>::type result<Tag>::ptr;
template <typename Tag, typename Tag::type p> struct rob : result<Tag> {
  struct filler { filler() { result<Tag>::ptr = p; } };
  static filler filler_obj;
};
template <typename Tag, typename Tag::type p> typename rob<Tag, p>::filler rob<Tag, p>::filler_obj;
struct CovTag { typedef Eigen::MatrixXd ov_msckf::State::*type; };
struct VarsTag { typedef std::vector<std::shared_ptr<ov_type::Type>> ov_msckf::State::*type; };
template struct rob<CovTag, &ov_msckf::State::_Cov>;
template struct rob<VarsTag, &ov_msckf::State::_variables>;
inline Eigen::MatrixXd &cov(ov_msckf::State &s) { return s.*result<CovTag>::ptr; }
inline const Eigen::MatrixXd &cov(const ov_msckf::State &s) { return s.*result<CovTag>::ptr; }
inline const std::vector<std::shared_ptr<ov_type::Type>> &vars(const ov_msckf::State &s) { return s.*result<VarsTag>::ptr; }
} // namespace c8_access

namespace {

// ---------------------------------------------------------------- config --
struct Config {
  bool loaded = false;
  std::string mode = "capture";
  int cls = 0;
  std::uint64_t period = 0;
  std::uint64_t phase = 0;
  std::uint64_t cycle_stride = 8;
  std::string log_path;
  int t0_stage = -1;
  int t0_kind = 0;
  std::uint64_t t0_at = 0;
  FILE *log = nullptr;
};
Config g_cfg;
std::mutex g_cfg_mutex;

const char *env_or(const char *name, const char *dflt) {
  const char *v = std::getenv(name);
  return (v && *v) ? v : dflt;
}

void load_config() {
  std::lock_guard<std::mutex> lock(g_cfg_mutex);
  if (g_cfg.loaded) return;
  g_cfg.mode = env_or("C8_MODE", "capture");
  g_cfg.cls = std::atoi(env_or("C8_CLASS", "0"));
  g_cfg.period = std::strtoull(env_or("C8_PERIOD", "0"), nullptr, 10);
  g_cfg.phase = std::strtoull(env_or("C8_PHASE", "0"), nullptr, 10);
  g_cfg.cycle_stride = std::strtoull(env_or("C8_CYCLE_STRIDE", "8"), nullptr, 10);
  g_cfg.log_path = env_or("C8_LOG", "");
  g_cfg.t0_stage = std::atoi(env_or("C8_T0_STAGE", "-1"));
  g_cfg.t0_kind = std::atoi(env_or("C8_T0_KIND", "0"));
  g_cfg.t0_at = std::strtoull(env_or("C8_T0_AT", "0"), nullptr, 10);
  if (!g_cfg.log_path.empty()) {
    g_cfg.log = std::fopen(g_cfg.log_path.c_str(), "a");
  }
  g_cfg.loaded = true;
}

void log_line(const std::string &s) {
  if (!g_cfg.log) return;
  std::fwrite(s.data(), 1, s.size(), g_cfg.log);
  std::fputc('\n', g_cfg.log);
  std::fflush(g_cfg.log);
}

// ------------------------------------------------------------- real syms --
template <typename Fn> Fn real_symbol(const char *mangled) {
  void *p = dlsym(RTLD_NEXT, mangled);
  if (!p) {
    std::fprintf(stderr, "[C8-SHIM] FATAL: cannot resolve %s: %s\n", mangled, dlerror());
    std::abort();
  }
  return reinterpret_cast<Fn>(p);
}

using UpdateFn = void (*)(ov_msckf::UpdaterMSCKF *, std::shared_ptr<ov_msckf::State>,
                          std::vector<std::shared_ptr<ov_core::Feature>> &);
using JacobianFn = void (*)(std::shared_ptr<ov_msckf::State>,
                            ov_msckf::UpdaterHelper::UpdaterHelperFeature &,
                            Eigen::MatrixXd &, Eigen::MatrixXd &, Eigen::VectorXd &,
                            std::vector<std::shared_ptr<ov_type::Type>> &);
using CommitFn = bool (*)(std::shared_ptr<ov_msckf::State>, const Eigen::VectorXd &,
                          const Eigen::MatrixXd &,
                          ov_msckf::StateHelper::PrecomputedCovariancePolicy);
using PreviewFn = ov_msckf::MSCKFUpdatePreviewResult (*)(
    const ov_msckf::MSCKFUpdatePreviewSnapshot &,
    const std::vector<ov_msckf::MSCKFUpdatePreviewBlock> &, const Eigen::MatrixXd &,
    const Eigen::VectorXd &, const Eigen::MatrixXd &);
using SetCallbackFn = bool (*)(ov_msckf::UpdaterMSCKF *,
                               std::function<void(ov_msckf::CP2LiveUpdateEvent)>, bool);
using TurnSafeDiagnosticFaultStage_t = ov_msckf::TurnSafeDiagnosticFaultStage;
using TurnSafeDiagnosticFaultKind_t = ov_msckf::TurnSafeDiagnosticFaultKind;
using SetT0FaultFn = void (*)(ov_msckf::TurnSafeDiagnosticFaultStage,
                              ov_msckf::TurnSafeDiagnosticFaultKind);
using ClearT0FaultFn = void (*)();

const char *kUpdateSym =
    "_ZN8ov_msckf12UpdaterMSCKF6updateESt10shared_ptrINS_5StateEERSt6vectorIS1_IN7ov_core7FeatureEESaIS7_EE";
const char *kJacobianSym =
    "_ZN8ov_msckf13UpdaterHelper25get_feature_jacobian_fullESt10shared_ptrINS_5StateEERNS0_20UpdaterHelperFeatureERN5Eigen6MatrixIdLin1ELin1ELi0ELin1ELin1EEES9_RNS7_IdLin1ELi1ELi0ELin1ELi1EEERSt6vectorIS1_IN7ov_type4TypeEESaISF_EE";
const char *kCommitSym =
    "_ZN8ov_msckf11StateHelper23CommitPrecomputedUpdateESt10shared_ptrINS_5StateEERKN5Eigen6MatrixIdLin1ELi1ELi0ELin1ELi1EEERKNS5_IdLin1ELin1ELi0ELin1ELin1EEENS0_27PrecomputedCovariancePolicyE";
const char *kPreviewSym =
    "_ZN8ov_msckf19UpdaterMSCKFPreview19ComputeFromSnapshotERKNS_26MSCKFUpdatePreviewSnapshotERKSt6vectorINS_23MSCKFUpdatePreviewBlockESaIS5_EERKN5Eigen6MatrixIdLin1ELin1ELi0ELin1ELin1EEERKNSB_IdLin1ELi1ELi0ELin1ELi1EEESE_";
const char *kSetCallbackSym =
    "_ZN8ov_msckf12UpdaterMSCKF23set_cp2_update_callbackESt8functionIFvNS_18CP2LiveUpdateEventEEEb";
const char *kSetT0FaultSym =
    "_ZN8ov_msckf38set_turnsafe_diagnostic_fault_for_testENS_28TurnSafeDiagnosticFaultStageENS_27TurnSafeDiagnosticFaultKindE";
const char *kClearT0FaultSym = "_ZN8ov_msckf40clear_turnsafe_diagnostic_fault_for_testEv";

// ------------------------------------------------------------ state hash --
std::string hex(const unsigned char *d, std::size_t n) {
  static const char *k = "0123456789abcdef";
  std::string s;
  s.reserve(2 * n);
  for (std::size_t i = 0; i < n; ++i) {
    s.push_back(k[d[i] >> 4]);
    s.push_back(k[d[i] & 15]);
  }
  return s;
}

struct Hasher {
  EVP_MD_CTX *ctx;
  Hasher() : ctx(EVP_MD_CTX_new()) { EVP_DigestInit_ex(ctx, EVP_sha256(), nullptr); }
  ~Hasher() { EVP_MD_CTX_free(ctx); }
  void bytes(const void *p, std::size_t n) { EVP_DigestUpdate(ctx, p, n); }
  void u64(std::uint64_t v) { bytes(&v, sizeof(v)); }
  void f64(double v) { bytes(&v, sizeof(v)); }
  void mat(const Eigen::MatrixXd &m) {
    u64(static_cast<std::uint64_t>(m.rows()));
    u64(static_cast<std::uint64_t>(m.cols()));
    // Column-major contiguous storage for MatrixXd.
    bytes(m.data(), sizeof(double) * static_cast<std::size_t>(m.size()));
  }
  std::string hexdigest() {
    unsigned char out[EVP_MAX_MD_SIZE];
    unsigned int n = 0;
    EVP_DigestFinal_ex(ctx, out, &n);
    return hex(out, n);
  }
};

// Hash of the complete mutable estimator state visible at the envelope
// boundary: timestamp, full covariance, and every variable's nominal + FEJ
// values in covariance order, plus the clone timestamp keys.
std::string hash_state(const ov_msckf::State &s) {
  Hasher h;
  h.f64(s._timestamp);
  h.mat(c8_access::cov(s));
  h.u64(c8_access::vars(s).size());
  for (const auto &v : c8_access::vars(s)) {
    if (!v) { h.u64(0xdeadbeef); continue; }
    h.u64(static_cast<std::uint64_t>(v->id()));
    h.u64(static_cast<std::uint64_t>(v->size()));
    h.mat(v->value());
    h.mat(v->fej());
  }
  h.u64(s._clones_IMU.size());
  for (const auto &kv : s._clones_IMU) h.f64(kv.first);
  h.u64(s._features_SLAM.size());
  return h.hexdigest();
}

std::string cov_hash(const ov_msckf::State &s) {
  Hasher h;
  h.mat(c8_access::cov(s));
  return h.hexdigest();
}

// ------------------------------------------------------- per-call context --
struct Context {
  bool active = false;
  std::uint64_t invocation = 0;
  ov_msckf::State *state = nullptr;
  int cls = 0;                 // injected class this invocation (0 = none)
  bool factor_pending = false; // classes 1-3 act on the first jacobian
  bool factor_done = false;
  std::size_t factor_feature_id = 0;
  bool snapshot_corrupt_pending = false; // class 4
  bool snapshot_corrupt_done = false;
  bool prior_change_pending = false; // class 6
  bool prior_change_done = false;
  bool commit_corrupt_pending = false; // class 8
  bool commit_corrupt_done = false;
  std::uint64_t jacobian_calls = 0;
  std::uint64_t preview_calls = 0;
  std::uint64_t preview_accepted = 0;
  std::string preview_status;
  std::string preview_stage;
  std::uint64_t commit_calls = 0;
  std::uint64_t commit_true = 0;
  std::uint64_t commit_false = 0;
  bool event_seen = false;
  ov_msckf::CP2LiveUpdateEvent event;
  // saved values for harness-side restoration of harness-side corruption
  Eigen::MatrixXd saved_cov;
  Eigen::MatrixXd saved_imu_value;
  bool saved_cov_valid = false;
  bool saved_imu_valid = false;
};
thread_local Context t_ctx;
std::atomic<std::uint64_t> g_invocations{0};
std::atomic<std::uint64_t> g_eligible{0};
std::atomic<bool> g_callback_installed{false};
std::atomic<bool> g_t0_armed{false};

const char *class_name(int c) {
  switch (c) {
  case 1: return "invalid_factor_dimensions";
  case 2: return "nonfinite_factor_values";
  case 3: return "rank_failure";
  case 4: return "innovation_factorization_failure";
  case 5: return "negative_posterior_diagonal";
  case 6: return "prior_changed_between_preview_and_commit";
  case 7: return "logging_diagnostics_failure";
  case 8: return "feature_factor_failure_after_proposal";
  default: return "none";
  }
}

int class_for_invocation(std::uint64_t n) {
  if (g_cfg.mode == "single") {
    if (g_cfg.period == 0) return 0;
    return (n % g_cfg.period == g_cfg.phase) ? g_cfg.cls : 0;
  }
  if (g_cfg.mode == "cycle") {
    if (g_cfg.cycle_stride == 0 || n % g_cfg.cycle_stride != 0) return 0;
    static const int kCycle[7] = {1, 2, 3, 4, 5, 6, 8};
    return kCycle[(n / g_cfg.cycle_stride) % 7];
  }
  return 0;
}

const char *terminal_status_name(ov_msckf::CP2UpdateTerminalStatus s) {
  using T = ov_msckf::CP2UpdateTerminalStatus;
  switch (s) {
  case T::kCommittedCounted: return "committed_counted";
  case T::kPreflightRejected: return "preflight_rejected";
  case T::kInternalFailure: return "internal_failure";
  default: break;
  }
  return "other";
}

std::string json_escape(const std::string &s) {
  std::string o;
  for (char c : s) {
    if (c == '"' || c == '\\') { o.push_back('\\'); o.push_back(c); }
    else if (c == '\n') o += "\\n";
    else o.push_back(c);
  }
  return o;
}

// Load-time arming for class-7 stages that fire before the first update
// (sink open/fdopen/header). C8_T0_AT=0 selects this path.
__attribute__((constructor)) static void c8_shim_load_time_arm() {
  const char *at = std::getenv("C8_T0_AT");
  const char *stage = std::getenv("C8_T0_STAGE");
  if (!at || !stage || std::strcmp(at, "0") != 0 || std::atoi(stage) < 0) return;
  void *p = dlsym(RTLD_DEFAULT, kSetT0FaultSym);
  if (!p) return; // not an estimator process
  load_config();
  reinterpret_cast<SetT0FaultFn>(p)(static_cast<TurnSafeDiagnosticFaultStage_t>(g_cfg.t0_stage),
                                    static_cast<TurnSafeDiagnosticFaultKind_t>(g_cfg.t0_kind));
  g_t0_armed.store(true);
  log_line("{\"kind\":\"t0_fault_armed\",\"invocation\":0,\"stage\":" + std::to_string(g_cfg.t0_stage) +
           ",\"fault_kind\":" + std::to_string(g_cfg.t0_kind) + "}");
}

} // namespace

// ============================================================ hooks ======
namespace ov_msckf {

void UpdaterMSCKF::update(std::shared_ptr<State> state,
                          std::vector<std::shared_ptr<ov_core::Feature>> &feature_vec) {
  static UpdateFn real = real_symbol<UpdateFn>(kUpdateSym);
  static SetCallbackFn set_cb = real_symbol<SetCallbackFn>(kSetCallbackSym);
  load_config();

  // Observer: installed before the first real update freezes the
  // callback configuration. Pure observation (shadow disabled).
  if (!g_callback_installed.exchange(true)) {
    const bool ok = set_cb(this,
                           [](CP2LiveUpdateEvent ev) {
                             if (t_ctx.active) {
                               t_ctx.event = ev;
                               t_ctx.event_seen = true;
                             }
                           },
                           false);
    log_line(std::string("{\"kind\":\"shim_init\",\"callback_installed\":") +
             (ok ? "true" : "false") + ",\"mode\":\"" + g_cfg.mode + "\"}");
  }

  const std::uint64_t n = ++g_invocations;
  Context &c = t_ctx;
  c = Context();
  c.active = true;
  c.invocation = n;
  c.state = state.get();
  const std::size_t features_in = feature_vec.size();
  std::uint64_t eligible_index = 0;
  if (features_in > 0) {
    eligible_index = ++g_eligible;
    c.cls = class_for_invocation(eligible_index);
  }
  const std::string h_pre_original = hash_state(*state);
  std::string h_pre_presented = h_pre_original;
  const double ts = state->_timestamp;
  const int dim = state->max_covariance_size();

  // Class-7 arming (diagnostics fault): set the pending fault once at the
  // requested invocation; the estimator's T0 stream consumes it.
  if (g_cfg.t0_stage >= 0 && n == g_cfg.t0_at && !g_t0_armed.exchange(true)) {
    static SetT0FaultFn set_fault = real_symbol<SetT0FaultFn>(kSetT0FaultSym);
    set_fault(static_cast<TurnSafeDiagnosticFaultStage>(g_cfg.t0_stage),
              static_cast<TurnSafeDiagnosticFaultKind>(g_cfg.t0_kind));
    log_line("{\"kind\":\"t0_fault_armed\",\"invocation\":" + std::to_string(n) +
             ",\"stage\":" + std::to_string(g_cfg.t0_stage) +
             ",\"fault_kind\":" + std::to_string(g_cfg.t0_kind) + "}");
  }

  // Pre-call injections (envelope-boundary corruption of the prior).
  if (c.cls == 1 || c.cls == 2 || c.cls == 3) {
    c.factor_pending = true;
  } else if (c.cls == 4) {
    c.snapshot_corrupt_pending = true;
  } else if (c.cls == 5) {
    c.saved_cov = c8_access::cov(*state);
    c.saved_cov_valid = true;
    if (dim > 15) {
      const double f = 1.0e6;
      c8_access::cov(*state).block(0, 15, 15, dim - 15) *= f;
      c8_access::cov(*state).block(15, 0, dim - 15, 15) *= f;
    }
    h_pre_presented = hash_state(*state);
  } else if (c.cls == 6) {
    c.prior_change_pending = true;
  } else if (c.cls == 8) {
    c.commit_corrupt_pending = true;
  }

  std::string exception_text;
  bool threw = false;
  try {
    real(this, state, feature_vec);
  } catch (const std::exception &e) {
    threw = true;
    exception_text = e.what();
  } catch (...) {
    threw = true;
    exception_text = "non-std exception";
  }

  const std::string h_post = hash_state(*state);
  const bool state_unchanged_vs_presented = (h_post == h_pre_presented);

  // Harness-side restoration of harness-side corruption, only when the
  // estimator provably did not commit (state hash equals the presented
  // prior). If a commit happened on a corrupted prior we leave it, record
  // it, and it will show as fault_triggered=false with a commit.
  bool restored = false;
  bool containment_restore = false;
  if (c.saved_cov_valid && state_unchanged_vs_presented) {
    c8_access::cov(*state) = c.saved_cov;
    restored = true;
  } else if (c.saved_cov_valid && c.commit_true > 0) {
    // The corrupted live prior did NOT realize the fault class (the preview
    // accepted it) and the estimator committed on it. That is an ineffective
    // injection, not a violation. To keep the replay alive for later
    // injections the harness restores the pre-corruption covariance
    // (harness-side containment; recorded, excluded from all verdict tallies).
    c8_access::cov(*state) = c.saved_cov;
    containment_restore = true;
  }
  if (c.saved_imu_valid && state_unchanged_vs_presented) {
    state->_imu->set_value(c.saved_imu_value);
    restored = true;
  }
  const std::string h_final = (restored || containment_restore) ? hash_state(*state) : h_post;

  // Fault triggered? (typed rejection observable for the class)
  bool fault_triggered = false;
  switch (c.cls) {
  case 1: case 2: case 3:
    fault_triggered = c.factor_done; // typed [MSCKF-SCHUR] is confirmed from console log by the driver
    break;
  case 4:
    fault_triggered = c.snapshot_corrupt_done && c.preview_status == "factorization_failed" && c.commit_calls == 0;
    break;
  case 5:
    fault_triggered = c.preview_calls > 0 && c.preview_status == "negative_diagonal";
    break;
  case 6:
    fault_triggered = c.prior_change_done && c.commit_calls == 0;
    break;
  case 8:
    fault_triggered = c.commit_corrupt_done && c.commit_true == 0 && c.commit_false > 0;
    break;
  default: break;
  }

  std::string rec = "{\"kind\":\"invocation\"";
  rec += ",\"invocation\":" + std::to_string(n);
  rec += ",\"eligible_index\":" + std::to_string(eligible_index);
  rec += ",\"state_timestamp\":" + std::to_string(ts);
  {
    char buf[64];
    std::snprintf(buf, sizeof(buf), "%.9f", ts);
    rec += ",\"state_timestamp_s\":\"" + std::string(buf) + "\"";
  }
  rec += ",\"class\":" + std::to_string(c.cls);
  rec += ",\"class_name\":\"" + std::string(class_name(c.cls)) + "\"";
  rec += ",\"features_in\":" + std::to_string(features_in);
  rec += ",\"features_out\":" + std::to_string(feature_vec.size());
  rec += ",\"cov_dim\":" + std::to_string(dim);
  rec += ",\"hash_pre_original\":\"" + h_pre_original + "\"";
  rec += ",\"hash_pre_presented\":\"" + h_pre_presented + "\"";
  rec += ",\"hash_post\":\"" + h_post + "\"";
  rec += ",\"hash_final\":\"" + h_final + "\"";
  rec += std::string(",\"state_unchanged_vs_presented\":") + (state_unchanged_vs_presented ? "true" : "false");
  rec += std::string(",\"restored_by_harness\":") + (restored ? "true" : "false");
  rec += std::string(",\"containment_restore\":") + (containment_restore ? "true" : "false");
  rec += std::string(",\"restored_equals_original\":") + ((restored && h_final == h_pre_original) ? "true" : "false");
  rec += ",\"jacobian_calls\":" + std::to_string(c.jacobian_calls);
  rec += ",\"factor_injected_feature\":" + std::to_string(c.factor_done ? c.factor_feature_id : 0);
  rec += std::string(",\"factor_injected\":") + (c.factor_done ? "true" : "false");
  rec += std::string(",\"snapshot_corrupt_injected\":") + (c.snapshot_corrupt_done ? "true" : "false");
  rec += std::string(",\"prior_change_injected\":") + (c.prior_change_done ? "true" : "false");
  rec += std::string(",\"commit_corrupt_injected\":") + (c.commit_corrupt_done ? "true" : "false");
  rec += ",\"preview_calls\":" + std::to_string(c.preview_calls);
  rec += ",\"preview_status\":\"" + c.preview_status + "\"";
  rec += ",\"preview_stage\":\"" + c.preview_stage + "\"";
  rec += ",\"commit_calls\":" + std::to_string(c.commit_calls);
  rec += ",\"commit_true\":" + std::to_string(c.commit_true);
  rec += ",\"commit_false\":" + std::to_string(c.commit_false);
  rec += std::string(",\"event_seen\":") + (c.event_seen ? "true" : "false");
  if (c.event_seen) {
    rec += ",\"terminal_status\":\"" + std::string(terminal_status_name(c.event.terminal_status)) + "\"";
    rec += ",\"terminal_status_code\":" + std::to_string(static_cast<int>(c.event.terminal_status));
    rec += ",\"terminal_subreason_code\":" + std::to_string(static_cast<int>(c.event.terminal_subreason));
    rec += std::string(",\"preflight_attempted\":") + (c.event.baseline_preflight_attempted ? "true" : "false");
    rec += std::string(",\"preflight_accepted\":") + (c.event.baseline_preflight_accepted ? "true" : "false");
    rec += std::string(",\"commit_occurred\":") + (c.event.baseline_commit_occurred ? "true" : "false");
    rec += ",\"raw_system_count\":" + std::to_string(c.event.raw_system_count);
    rec += ",\"accepted_ids\":" + std::to_string(c.event.baseline_accepted_ids.size());
    rec += ",\"input_feature_count\":" + std::to_string(c.event.input_feature_count);
  }
  rec += std::string(",\"fault_triggered\":") + (fault_triggered ? "true" : "false");
  rec += std::string(",\"threw\":") + (threw ? "true" : "false");
  rec += ",\"exception\":\"" + json_escape(exception_text) + "\"";
  rec += "}";
  log_line(rec);
  c.active = false;
  if (threw) throw std::runtime_error("[C8-SHIM] rethrow: " + exception_text);
}

void UpdaterHelper::get_feature_jacobian_full(std::shared_ptr<State> state,
                                              UpdaterHelperFeature &feature,
                                              Eigen::MatrixXd &H_f, Eigen::MatrixXd &H_x,
                                              Eigen::VectorXd &res,
                                              std::vector<std::shared_ptr<ov_type::Type>> &x_order) {
  static JacobianFn real = real_symbol<JacobianFn>(kJacobianSym);
  real(state, feature, H_f, H_x, res, x_order);
  Context &c = t_ctx;
  if (!c.active) return;
  ++c.jacobian_calls;
  if (c.factor_pending && !c.factor_done) {
    c.factor_done = true;
    c.factor_pending = false;
    c.factor_feature_id = feature.featid;
    switch (c.cls) {
    case 1:
      if (res.rows() > 1) res.conservativeResize(res.rows() - 1);
      break;
    case 2:
      if (res.rows() > 0) res(0) = std::numeric_limits<double>::quiet_NaN();
      break;
    case 3:
      if (H_f.cols() >= 3) H_f.col(2) = H_f.col(1);
      break;
    default: break;
    }
  }
}

MSCKFUpdatePreviewResult UpdaterMSCKFPreview::ComputeFromSnapshot(
    const MSCKFUpdatePreviewSnapshot &snapshot,
    const std::vector<MSCKFUpdatePreviewBlock> &jacobian_layout, const Eigen::MatrixXd &H,
    const Eigen::VectorXd &residual, const Eigen::MatrixXd &R) {
  static PreviewFn real = real_symbol<PreviewFn>(kPreviewSym);
  Context &c = t_ctx;
  if (!c.active) return real(snapshot, jacobian_layout, H, residual, R);
  MSCKFUpdatePreviewResult r;
  if (c.snapshot_corrupt_pending && !c.snapshot_corrupt_done) {
    c.snapshot_corrupt_done = true;
    c.snapshot_corrupt_pending = false;
    MSCKFUpdatePreviewSnapshot bad = snapshot;
    bad.covariance = -1.0e6 * bad.covariance; // S = H P H' + R becomes negative definite
    r = real(bad, jacobian_layout, H, residual, R);
  } else {
    r = real(snapshot, jacobian_layout, H, residual, R);
  }
  ++c.preview_calls;
  if (r.accepted()) ++c.preview_accepted;
  {
    using S = MSCKFUpdatePreviewStatus;
    switch (r.diagnostics.status) {
    case S::kAccepted: c.preview_status = "accepted"; break;
    case S::kInvalidInput: c.preview_status = "invalid_input"; break;
    case S::kNonfinite: c.preview_status = "nonfinite"; break;
    case S::kFactorizationFailed: c.preview_status = "factorization_failed"; break;
    case S::kNegativeDiagonal: c.preview_status = "negative_diagonal"; break;
    default: c.preview_status = "other"; break;
    }
    c.preview_stage = std::to_string(static_cast<int>(r.diagnostics.stage));
  }
  if (c.prior_change_pending && !c.prior_change_done && r.accepted() && c.state && c.state->_imu) {
    c.prior_change_done = true;
    c.prior_change_pending = false;
    c.saved_imu_value = c.state->_imu->value();
    c.saved_imu_valid = true;
    Eigen::MatrixXd v = c.saved_imu_value;
    if (v.rows() > 4) v(4, 0) += 1.0e-3; // position x nominal perturbation
    c.state->_imu->set_value(v);
  }
  return r;
}

bool StateHelper::CommitPrecomputedUpdate(std::shared_ptr<State> state, const Eigen::VectorXd &dx,
                                          const Eigen::MatrixXd &posterior_covariance,
                                          PrecomputedCovariancePolicy policy) {
  static CommitFn real = real_symbol<CommitFn>(kCommitSym);
  Context &c = t_ctx;
  if (!c.active) return real(state, dx, posterior_covariance, policy);
  ++c.commit_calls;
  bool ok;
  if (c.commit_corrupt_pending && !c.commit_corrupt_done) {
    c.commit_corrupt_done = true;
    c.commit_corrupt_pending = false;
    Eigen::VectorXd bad = dx;
    if (bad.rows() > 0) bad(0) = std::numeric_limits<double>::quiet_NaN();
    ok = real(state, bad, posterior_covariance, policy);
  } else {
    ok = real(state, dx, posterior_covariance, policy);
  }
  if (ok) ++c.commit_true; else ++c.commit_false;
  return ok;
}

} // namespace ov_msckf
