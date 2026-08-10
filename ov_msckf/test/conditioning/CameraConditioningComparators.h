/*
 * Offline comparators for captured SchurVIO-Lite camera-track systems.
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * This test/analysis API deliberately lives outside the production updater.
 * It calls the production reducers but cannot change estimator state.
 */

#ifndef OV_MSCKF_TEST_CAMERA_CONDITIONING_COMPARATORS_H
#define OV_MSCKF_TEST_CAMERA_CONDITIONING_COMPARATORS_H

#include <Eigen/Core>

#include <cstdint>
#include <limits>
#include <string>
#include <vector>

namespace ov_msckf {
namespace conditioning {

constexpr double kFrozenHarmfulRelativeThreshold = 1.0e-3;
constexpr double kFrozenMinimumSingularRatio = 1.0e-6;

enum class Method {
  kUpstreamNullspace,
  kLocalNullspace,
  kGuardedNullspaceDrop,
  kRankAwareNullspace,
  kLocalSchur,
  kFullJointOracle,
  kFullUNullspaceOracle,
};

const char *method_name(Method method) noexcept;

/// One already validated, per-track raw camera system from capture schema 1.
struct CameraSystem {
  Eigen::MatrixXd H_x;
  Eigen::MatrixXd H_f;
  Eigen::VectorXd residual;
  Eigen::MatrixXd P_active;
  double sigma_px = std::numeric_limits<double>::quiet_NaN();
};

/// Frozen rank diagnostics evaluated on the explicitly whitened H_f.
struct RankDiagnostics {
  bool valid = false;
  bool singular_values_available = false;
  bool singular_ratio_available = false;
  Eigen::Vector3d singular_values =
      Eigen::Vector3d::Constant(std::numeric_limits<double>::quiet_NaN());
  int numerical_rank = 0;
  double numerical_rank_floor = std::numeric_limits<double>::quiet_NaN();
  double singular_ratio = std::numeric_limits<double>::quiet_NaN();
  bool frozen_guard_accepts = false;
  std::string guard_status;
  std::string guard_stage;
};

struct InformationMetrics {
  double supported_subspace_trace = std::numeric_limits<double>::quiet_NaN();
  double log_pseudodeterminant = std::numeric_limits<double>::quiet_NaN();
  double half_logdet_identity_plus_information =
      std::numeric_limits<double>::quiet_NaN();
  int effective_rank = 0;
  double correction_norm = std::numeric_limits<double>::quiet_NaN();
  double nis = std::numeric_limits<double>::quiet_NaN();
};

struct Posterior {
  bool valid = false;
  Eigen::VectorXd dx;
  Eigen::MatrixXd covariance;
  double nis = std::numeric_limits<double>::quiet_NaN();
  double minimum_covariance_eigenvalue = std::numeric_limits<double>::quiet_NaN();
  double psd_tolerance = std::numeric_limits<double>::quiet_NaN();
  double raw_symmetry_error_inf = std::numeric_limits<double>::quiet_NaN();
};

struct MethodResult {
  Method method = Method::kLocalSchur;
  bool accepted = false;
  std::string status;
  std::string reason;

  Eigen::MatrixXd H_reduced;
  Eigen::VectorXd residual_reduced;
  Eigen::MatrixXd lambda;
  Eigen::VectorXd eta;
  double gamma = std::numeric_limits<double>::quiet_NaN();
  Posterior posterior;
  InformationMetrics information;

  bool finite = false;
  bool input_mutation = false;
  bool mutation_expected = false;
  bool unexplained_input_mutation = false;

  double state_increment_relative_error = std::numeric_limits<double>::quiet_NaN();
  double posterior_covariance_relative_error = std::numeric_limits<double>::quiet_NaN();
  double nis_relative_error = std::numeric_limits<double>::quiet_NaN();
  bool scaled_psd_failure = false;
  bool harmful_accepted = false;
};

/// Oracle agreement is kept separate from per-method safety and guard action.
struct OracleAgreement {
  bool full_u_valid = false;
  bool full_joint_valid = false;
  double state_increment_relative_error = std::numeric_limits<double>::quiet_NaN();
  double posterior_covariance_relative_error = std::numeric_limits<double>::quiet_NaN();
  bool full_u_scaled_psd_failure = false;
  bool full_joint_scaled_psd_failure = false;
  bool oracle_safe_opportunity = false;
};

struct Comparison {
  RankDiagnostics rank;
  OracleAgreement oracle_agreement;
  std::vector<MethodResult> methods;

  /// Fixed hazard label used only by the guard-classification table.
  bool unguarded_nullspace_harmful = false;
  bool caller_input_mutated = false;
};

/// Validate shapes/finiteness and evaluate all seven methods in fixed order.
Comparison evaluate_all(const CameraSystem &system);

/// Evaluate one method against the supplied numerical reference, when present.
/// evaluate_all() uses FULL_JOINT for candidate safety and keeps FULL_U only
/// for the independent oracle-agreement check.
MethodResult evaluate_method(const CameraSystem &system, Method method,
                             const MethodResult *reference = nullptr);

/// Exposed for focused tests of the exact frozen equality semantics.
RankDiagnostics evaluate_frozen_rank_guard(const CameraSystem &system);

/// Frozen six-clause harmful predicate. It is false for rejected results.
bool frozen_harmful_acceptance(const MethodResult &result) noexcept;

} // namespace conditioning
} // namespace ov_msckf

#endif // OV_MSCKF_TEST_CAMERA_CONDITIONING_COMPARATORS_H
