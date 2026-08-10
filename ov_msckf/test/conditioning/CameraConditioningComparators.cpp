/*
 * Offline comparators for captured SchurVIO-Lite camera-track systems.
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include "CameraConditioningComparators.h"

#include "update/SchurUpdate.h"
#include "update/UpdaterHelper.h"

#include <Eigen/Cholesky>
#include <Eigen/Eigenvalues>
#include <Eigen/QR>
#include <Eigen/SVD>

#include <algorithm>
#include <cmath>
#include <cstring>
#include <limits>
#include <stdexcept>
#include <utility>

namespace ov_msckf {
namespace conditioning {
namespace {

constexpr double kRelativeDenominatorFloor = 1.0e-12;

struct PriorSupport {
  bool valid = false;
  Eigen::MatrixXd factor;
};

double matrix_inf_norm(const Eigen::MatrixXd &matrix) {
  if (matrix.size() == 0) {
    return 0.0;
  }
  return matrix.cwiseAbs().rowwise().sum().maxCoeff();
}

double relative_error(const Eigen::VectorXd &value, const Eigen::VectorXd &reference) {
  if (value.rows() != reference.rows() || !value.allFinite() || !reference.allFinite()) {
    return std::numeric_limits<double>::infinity();
  }
  return (value - reference).norm() /
         std::max(kRelativeDenominatorFloor, reference.norm());
}

double relative_error(const Eigen::MatrixXd &value, const Eigen::MatrixXd &reference) {
  if (value.rows() != reference.rows() || value.cols() != reference.cols() ||
      !value.allFinite() || !reference.allFinite()) {
    return std::numeric_limits<double>::infinity();
  }
  return (value - reference).norm() /
         std::max(kRelativeDenominatorFloor, reference.norm());
}

double relative_error(double value, double reference) {
  if (!std::isfinite(value) || !std::isfinite(reference)) {
    return std::numeric_limits<double>::infinity();
  }
  return std::abs(value - reference) /
         std::max(kRelativeDenominatorFloor, std::abs(reference));
}

bool bitwise_equal(const Eigen::MatrixXd &left, const Eigen::MatrixXd &right) {
  return left.rows() == right.rows() && left.cols() == right.cols() &&
         (left.size() == 0 ||
          std::memcmp(left.data(), right.data(),
                      static_cast<std::size_t>(left.size()) * sizeof(double)) == 0);
}

bool bitwise_equal(const Eigen::VectorXd &left, const Eigen::VectorXd &right) {
  return left.rows() == right.rows() &&
         (left.size() == 0 ||
          std::memcmp(left.data(), right.data(),
                      static_cast<std::size_t>(left.size()) * sizeof(double)) == 0);
}

bool valid_system_dimensions(const CameraSystem &system) {
  return system.H_x.rows() > 0 && system.H_x.cols() > 0 &&
         system.H_f.rows() == system.H_x.rows() && system.H_f.cols() == 3 &&
         system.residual.rows() == system.H_x.rows() &&
         system.P_active.rows() == system.H_x.cols() &&
         system.P_active.cols() == system.H_x.cols();
}

bool finite_system(const CameraSystem &system) {
  return valid_system_dimensions(system) && system.H_x.allFinite() &&
         system.H_f.allFinite() && system.residual.allFinite() &&
         system.P_active.allFinite() && std::isfinite(system.sigma_px) &&
         system.sigma_px > 0.0;
}

PriorSupport factor_prior_support(const Eigen::MatrixXd &covariance) {
  PriorSupport support;
  if (covariance.rows() != covariance.cols() || !covariance.allFinite()) {
    return support;
  }
  const Eigen::MatrixXd symmetric = 0.5 * (covariance + covariance.transpose());
  Eigen::SelfAdjointEigenSolver<Eigen::MatrixXd> eigensolver(symmetric);
  if (eigensolver.info() != Eigen::Success || !eigensolver.eigenvalues().allFinite() ||
      !eigensolver.eigenvectors().allFinite()) {
    return support;
  }
  const double max_abs = eigensolver.eigenvalues().size() == 0
                             ? 0.0
                             : eigensolver.eigenvalues().cwiseAbs().maxCoeff();
  const double psd_tolerance =
      256.0 * static_cast<double>(std::max<Eigen::Index>(1, covariance.rows())) *
      std::numeric_limits<double>::epsilon() * std::max(1.0, max_abs);
  if (matrix_inf_norm(covariance - covariance.transpose()) > psd_tolerance) {
    return support;
  }
  if (eigensolver.eigenvalues().size() > 0 &&
      eigensolver.eigenvalues().minCoeff() < -psd_tolerance) {
    return support;
  }

  // The support is frozen to the same scaled tolerance as the PSD decision.
  // This avoids treating roundoff-scale covariance modes as physical prior
  // support, and adds neither jitter nor eigenvalue clamping.
  const double support_floor = psd_tolerance;
  std::vector<Eigen::Index> positive;
  for (Eigen::Index index = 0; index < eigensolver.eigenvalues().rows(); ++index) {
    if (eigensolver.eigenvalues()(index) > support_floor) {
      positive.push_back(index);
    }
  }
  support.factor.resize(covariance.rows(), static_cast<Eigen::Index>(positive.size()));
  for (std::size_t column = 0; column < positive.size(); ++column) {
    const Eigen::Index index = positive[column];
    support.factor.col(static_cast<Eigen::Index>(column)) =
        std::sqrt(eigensolver.eigenvalues()(index)) * eigensolver.eigenvectors().col(index);
  }
  support.valid = support.factor.allFinite();
  return support;
}

void finalize_covariance_diagnostics(Posterior &posterior) {
  if (!posterior.covariance.allFinite()) {
    posterior.valid = false;
    return;
  }
  posterior.raw_symmetry_error_inf =
      matrix_inf_norm(posterior.covariance - posterior.covariance.transpose());
  posterior.covariance = 0.5 * (posterior.covariance + posterior.covariance.transpose());
  Eigen::SelfAdjointEigenSolver<Eigen::MatrixXd> eigensolver(posterior.covariance);
  if (eigensolver.info() != Eigen::Success || !eigensolver.eigenvalues().allFinite()) {
    posterior.valid = false;
    return;
  }
  posterior.minimum_covariance_eigenvalue =
      eigensolver.eigenvalues().size() == 0 ? 0.0 : eigensolver.eigenvalues().minCoeff();
  const double max_abs = eigensolver.eigenvalues().size() == 0
                             ? 0.0
                             : eigensolver.eigenvalues().cwiseAbs().maxCoeff();
  posterior.psd_tolerance =
      256.0 * static_cast<double>(std::max<Eigen::Index>(1, posterior.covariance.rows())) *
      std::numeric_limits<double>::epsilon() * std::max(1.0, max_abs);
}

InformationMetrics information_metrics(const Eigen::MatrixXd &H_whitened,
                                       const PriorSupport &support,
                                       const Posterior &posterior) {
  InformationMetrics metrics;
  if (!support.valid || H_whitened.cols() != support.factor.rows() ||
      !H_whitened.allFinite()) {
    return metrics;
  }
  const Eigen::MatrixXd supported_jacobian = H_whitened * support.factor;
  Eigen::MatrixXd information = supported_jacobian.transpose() * supported_jacobian;
  information = 0.5 * (information + information.transpose());
  Eigen::SelfAdjointEigenSolver<Eigen::MatrixXd> eigensolver(information);
  if (eigensolver.info() != Eigen::Success || !eigensolver.eigenvalues().allFinite()) {
    return metrics;
  }
  metrics.supported_subspace_trace = information.trace();
  const double largest = eigensolver.eigenvalues().size() == 0
                             ? 0.0
                             : eigensolver.eigenvalues().cwiseAbs().maxCoeff();
  const double floor =
      static_cast<double>(std::max<Eigen::Index>(1, information.rows())) *
      std::numeric_limits<double>::epsilon() * largest;
  metrics.log_pseudodeterminant = 0.0;
  metrics.half_logdet_identity_plus_information = 0.0;
  for (Eigen::Index index = 0; index < eigensolver.eigenvalues().rows(); ++index) {
    metrics.half_logdet_identity_plus_information +=
        0.5 * std::log1p(std::max(0.0, eigensolver.eigenvalues()(index)));
    if (eigensolver.eigenvalues()(index) > floor) {
      ++metrics.effective_rank;
      metrics.log_pseudodeterminant += std::log(eigensolver.eigenvalues()(index));
    }
  }
  metrics.correction_norm = posterior.dx.allFinite()
                                ? posterior.dx.norm()
                                : std::numeric_limits<double>::quiet_NaN();
  metrics.nis = posterior.nis;
  return metrics;
}

Posterior posterior_from_rows(const Eigen::MatrixXd &H_reduced,
                              const Eigen::VectorXd &residual_reduced,
                              double sigma_px, const PriorSupport &support) {
  Posterior posterior;
  if (!support.valid || !std::isfinite(sigma_px) || !(sigma_px > 0.0) ||
      H_reduced.rows() != residual_reduced.rows() ||
      H_reduced.cols() != support.factor.rows() || !H_reduced.allFinite() ||
      !residual_reduced.allFinite()) {
    return posterior;
  }
  const Eigen::MatrixXd H = H_reduced.array() / sigma_px;
  const Eigen::VectorXd residual = residual_reduced.array() / sigma_px;
  if (!H.allFinite() || !residual.allFinite()) {
    return posterior;
  }
  const Eigen::MatrixXd C = H * support.factor;
  Eigen::MatrixXd innovation = C * C.transpose();
  innovation.diagonal().array() += 1.0;
  Eigen::LLT<Eigen::MatrixXd> factor(innovation.selfadjointView<Eigen::Upper>());
  if (factor.info() != Eigen::Success) {
    return posterior;
  }
  const Eigen::VectorXd solved_residual = factor.solve(residual);
  const Eigen::MatrixXd solved_C = factor.solve(C);
  if (factor.info() != Eigen::Success || !solved_residual.allFinite() ||
      !solved_C.allFinite()) {
    return posterior;
  }
  const Eigen::VectorXd supported_dx = C.transpose() * solved_residual;
  Eigen::MatrixXd supported_covariance =
      Eigen::MatrixXd::Identity(support.factor.cols(), support.factor.cols()) -
      C.transpose() * solved_C;
  supported_covariance =
      0.5 * (supported_covariance + supported_covariance.transpose());
  posterior.dx = support.factor * supported_dx;
  posterior.covariance =
      support.factor * supported_covariance * support.factor.transpose();
  posterior.nis = residual.dot(solved_residual);
  posterior.valid = posterior.dx.allFinite() && posterior.covariance.allFinite() &&
                    std::isfinite(posterior.nis);
  finalize_covariance_diagnostics(posterior);
  return posterior;
}

void populate_from_rows(MethodResult &result, const CameraSystem &system,
                        const PriorSupport &support) {
  if (!result.accepted) {
    return;
  }
  const Eigen::MatrixXd H_whitened = result.H_reduced.array() / system.sigma_px;
  const Eigen::VectorXd residual_whitened =
      result.residual_reduced.array() / system.sigma_px;
  result.lambda = H_whitened.transpose() * H_whitened;
  result.lambda = 0.5 * (result.lambda + result.lambda.transpose());
  result.eta = H_whitened.transpose() * residual_whitened;
  result.gamma = residual_whitened.squaredNorm();
  result.posterior = posterior_from_rows(result.H_reduced, result.residual_reduced,
                                         system.sigma_px, support);
  result.information = information_metrics(H_whitened, support, result.posterior);
  result.finite = result.H_reduced.allFinite() && result.residual_reduced.allFinite() &&
                  result.lambda.allFinite() && result.eta.allFinite() &&
                  std::isfinite(result.gamma) && result.posterior.valid &&
                  result.posterior.dx.allFinite() && result.posterior.covariance.allFinite() &&
                  std::isfinite(result.posterior.nis) &&
                  std::isfinite(result.information.supported_subspace_trace) &&
                  std::isfinite(result.information.log_pseudodeterminant) &&
                  std::isfinite(result.information.half_logdet_identity_plus_information) &&
                  std::isfinite(result.information.correction_norm);
  result.scaled_psd_failure =
      result.posterior.valid &&
      result.posterior.minimum_covariance_eigenvalue < -result.posterior.psd_tolerance;
}

MethodResult production_nullspace(const CameraSystem &system, Method method,
                                  const PriorSupport &support) {
  MethodResult result;
  result.method = method;
  if (!finite_system(system) || system.H_f.rows() <= system.H_f.cols()) {
    result.status = "rejected";
    result.reason = "invalid_input_or_insufficient_rows";
    return result;
  }
  Eigen::MatrixXd H_x = system.H_x;
  Eigen::MatrixXd H_f = system.H_f;
  Eigen::VectorXd residual = system.residual;
  UpdaterHelper::nullspace_project_inplace(H_f, H_x, residual);
  result.accepted = true;
  result.status = "accepted_no_rank_guard";
  result.reason = "production_inplace_givens";
  result.H_reduced = H_x;
  result.residual_reduced = residual;
  result.input_mutation = !bitwise_equal(H_x, system.H_x) ||
                          !bitwise_equal(H_f, system.H_f) ||
                          !bitwise_equal(residual, system.residual);
  result.mutation_expected = true;
  result.unexplained_input_mutation = false;
  populate_from_rows(result, system, support);
  return result;
}

MethodResult guarded_nullspace(const CameraSystem &system,
                               const PriorSupport &support) {
  MethodResult result;
  result.method = Method::kGuardedNullspaceDrop;
  const RankDiagnostics guard = evaluate_frozen_rank_guard(system);
  if (!guard.frozen_guard_accepts) {
    result.status = guard.guard_status;
    result.reason = guard.guard_stage;
    return result;
  }
  result = production_nullspace(system, Method::kGuardedNullspaceDrop, support);
  if (result.accepted) {
    result.status = "accepted_guard_then_production_nullspace";
    result.reason = "frozen_rank_condition_guard_passed";
  }
  return result;
}

MethodResult rank_aware_nullspace(const CameraSystem &system, Method method,
                                  const PriorSupport &support) {
  MethodResult result;
  result.method = method;
  if (!finite_system(system)) {
    result.status = "rejected";
    result.reason = "invalid_input";
    return result;
  }
  const Eigen::MatrixXd B = system.H_f.array() / system.sigma_px;
  Eigen::JacobiSVD<Eigen::MatrixXd> svd(B, Eigen::ComputeFullU | Eigen::ComputeFullV);
  if (!svd.singularValues().allFinite() || !svd.matrixU().allFinite()) {
    result.status = "rejected";
    result.reason = "nonfinite_svd";
    return result;
  }
  int rank = 0;
  if (svd.singularValues().size() > 0) {
    const double largest = svd.singularValues()(0);
    if (largest > std::numeric_limits<double>::min()) {
      const double floor =
          static_cast<double>(std::max<Eigen::Index>(B.rows(), B.cols())) *
          std::numeric_limits<double>::epsilon() * largest;
      for (Eigen::Index index = 0; index < svd.singularValues().rows(); ++index) {
        if (svd.singularValues()(index) > floor) {
          ++rank;
        }
      }
    }
  }
  const Eigen::Index nullity = B.rows() - rank;
  if (nullity <= 0 || svd.matrixU().cols() != B.rows()) {
    result.status = "rejected";
    result.reason = "empty_left_nullspace";
    return result;
  }
  const Eigen::MatrixXd nullspace_transpose =
      svd.matrixU().rightCols(nullity).transpose();
  result.accepted = true;
  result.status = method == Method::kFullUNullspaceOracle
                      ? "accepted_full_u_oracle"
                      : "accepted_rank_aware_full_u";
  result.reason = "svd_numerical_rank_" + std::to_string(rank);
  result.H_reduced = nullspace_transpose * system.H_x;
  result.residual_reduced = nullspace_transpose * system.residual;
  populate_from_rows(result, system, support);
  return result;
}

MethodResult local_schur(const CameraSystem &system, const PriorSupport &support) {
  MethodResult result;
  result.method = Method::kLocalSchur;
  const Eigen::MatrixXd H_x_before = system.H_x;
  const Eigen::MatrixXd H_f_before = system.H_f;
  const Eigen::VectorXd residual_before = system.residual;
  const SchurReductionResult reduced =
      SchurUpdate::Reduce(system.H_x, system.H_f, system.residual, system.sigma_px);
  result.status = schur_reduction_status_name(reduced.status);
  result.reason = schur_reduction_stage_name(reduced.stage);
  result.input_mutation = !bitwise_equal(system.H_x, H_x_before) ||
                          !bitwise_equal(system.H_f, H_f_before) ||
                          !bitwise_equal(system.residual, residual_before);
  result.mutation_expected = false;
  result.unexplained_input_mutation = result.input_mutation;
  if (!reduced.accepted()) {
    return result;
  }
  result.accepted = true;
  result.H_reduced = reduced.H_reduced;
  result.residual_reduced = reduced.residual_reduced;
  populate_from_rows(result, system, support);
  return result;
}

MethodResult full_joint_oracle(const CameraSystem &system,
                               const PriorSupport &support) {
  MethodResult result;
  result.method = Method::kFullJointOracle;
  if (!finite_system(system) || !support.valid) {
    result.status = "rejected";
    result.reason = "invalid_input_or_prior";
    return result;
  }
  const Eigen::MatrixXd A = system.H_x.array() / system.sigma_px;
  const Eigen::MatrixXd B = system.H_f.array() / system.sigma_px;
  const Eigen::VectorXd b = system.residual.array() / system.sigma_px;
  const Eigen::MatrixXd A_supported = A * support.factor;
  const Eigen::Index support_rank = support.factor.cols();
  Eigen::MatrixXd joint = Eigen::MatrixXd::Zero(
      support_rank + A.rows(), support_rank + B.cols());
  joint.topLeftCorner(support_rank, support_rank).setIdentity();
  joint.block(support_rank, 0, A.rows(), support_rank) = A_supported;
  joint.block(support_rank, support_rank, B.rows(), B.cols()) = B;
  Eigen::VectorXd rhs = Eigen::VectorXd::Zero(support_rank + b.rows());
  rhs.tail(b.rows()) = b;
  Eigen::JacobiSVD<Eigen::MatrixXd> joint_svd(
      joint, Eigen::ComputeThinU | Eigen::ComputeThinV);
  if (!joint_svd.singularValues().allFinite() || !joint_svd.matrixU().allFinite() ||
      !joint_svd.matrixV().allFinite() || joint_svd.singularValues().size() == 0) {
    result.status = "rejected";
    result.reason = "joint_svd_failure";
    return result;
  }
  const double floor =
      static_cast<double>(std::max(joint.rows(), joint.cols())) *
      std::numeric_limits<double>::epsilon() * joint_svd.singularValues()(0);
  Eigen::VectorXd coefficients = joint_svd.matrixU().transpose() * rhs;
  Eigen::VectorXd inverse_squared =
      Eigen::VectorXd::Zero(joint_svd.singularValues().rows());
  for (Eigen::Index index = 0; index < joint_svd.singularValues().rows(); ++index) {
    const double singular = joint_svd.singularValues()(index);
    if (singular > floor) {
      coefficients(index) /= singular;
      inverse_squared(index) = 1.0 / (singular * singular);
    } else {
      coefficients(index) = 0.0;
    }
  }
  const Eigen::VectorXd solution = joint_svd.matrixV() * coefficients;
  const Eigen::MatrixXd joint_covariance =
      joint_svd.matrixV() * inverse_squared.asDiagonal() *
      joint_svd.matrixV().transpose();

  // A COD projector supplies an independent row representation for NIS and
  // retained-information metrics; state mean/covariance above come only from
  // the augmented full-joint SVD.
  Eigen::CompleteOrthogonalDecomposition<Eigen::MatrixXd> cod(B);
  const Eigen::MatrixXd B_pseudoinverse =
      cod.solve(Eigen::MatrixXd::Identity(B.rows(), B.rows()));
  Eigen::MatrixXd projector =
      Eigen::MatrixXd::Identity(B.rows(), B.rows()) - B * B_pseudoinverse;
  projector = 0.5 * (projector + projector.transpose());
  Eigen::SelfAdjointEigenSolver<Eigen::MatrixXd> projector_eigensolver(projector);
  if (projector_eigensolver.info() != Eigen::Success ||
      !projector_eigensolver.eigenvalues().allFinite()) {
    result.status = "rejected";
    result.reason = "joint_projector_failure";
    return result;
  }
  std::vector<Eigen::Index> null_columns;
  for (Eigen::Index index = 0; index < projector_eigensolver.eigenvalues().rows();
       ++index) {
    if (projector_eigensolver.eigenvalues()(index) > 0.5) {
      null_columns.push_back(index);
    }
  }
  Eigen::MatrixXd nullspace(B.rows(), static_cast<Eigen::Index>(null_columns.size()));
  for (std::size_t column = 0; column < null_columns.size(); ++column) {
    nullspace.col(static_cast<Eigen::Index>(column)) =
        projector_eigensolver.eigenvectors().col(null_columns[column]);
  }
  result.H_reduced = nullspace.transpose() * system.H_x;
  result.residual_reduced = nullspace.transpose() * system.residual;
  result.accepted = true;
  result.status = "accepted_full_joint_oracle";
  result.reason = "psd_support_augmented_svd";
  populate_from_rows(result, system, support);

  result.posterior.dx = support.factor * solution.head(support_rank);
  result.posterior.covariance =
      support.factor * joint_covariance.topLeftCorner(support_rank, support_rank) *
      support.factor.transpose();
  // The full-joint NIS is independently the minimized augmented squared
  // residual, including the supported-prior rows. It equals the marginalized
  // innovation statistic when both oracle constructions agree.
  result.posterior.nis = (joint * solution - rhs).squaredNorm();
  result.posterior.valid = result.posterior.dx.allFinite() &&
                           result.posterior.covariance.allFinite() &&
                           std::isfinite(result.posterior.nis);
  finalize_covariance_diagnostics(result.posterior);
  const Eigen::MatrixXd H_whitened = result.H_reduced.array() / system.sigma_px;
  result.information = information_metrics(H_whitened, support, result.posterior);
  result.finite = result.H_reduced.allFinite() && result.residual_reduced.allFinite() &&
                  result.lambda.allFinite() && result.eta.allFinite() &&
                  std::isfinite(result.gamma) && result.posterior.valid &&
                  result.posterior.dx.allFinite() && result.posterior.covariance.allFinite() &&
                  std::isfinite(result.posterior.nis) &&
                  std::isfinite(result.information.supported_subspace_trace) &&
                  std::isfinite(result.information.log_pseudodeterminant) &&
                  std::isfinite(result.information.half_logdet_identity_plus_information) &&
                  std::isfinite(result.information.correction_norm);
  result.scaled_psd_failure =
      result.posterior.valid &&
      result.posterior.minimum_covariance_eigenvalue < -result.posterior.psd_tolerance;
  return result;
}

void compare_with_reference(MethodResult &result, const MethodResult &reference) {
  if (!result.accepted) {
    return;
  }
  result.state_increment_relative_error =
      relative_error(result.posterior.dx, reference.posterior.dx);
  result.posterior_covariance_relative_error =
      relative_error(result.posterior.covariance, reference.posterior.covariance);
  result.nis_relative_error = relative_error(result.posterior.nis, reference.posterior.nis);
  result.harmful_accepted = frozen_harmful_acceptance(result);
}

} // namespace

const char *method_name(Method method) noexcept {
  switch (method) {
  case Method::kUpstreamNullspace:
    return "U_NS";
  case Method::kLocalNullspace:
    return "L_NS";
  case Method::kGuardedNullspaceDrop:
    return "GUARDED_NS_DROP";
  case Method::kRankAwareNullspace:
    return "RANK_AWARE_NS";
  case Method::kLocalSchur:
    return "L_SCHUR";
  case Method::kFullJointOracle:
    return "FULL_JOINT_ORACLE";
  case Method::kFullUNullspaceOracle:
    return "FULL_U_NULLSPACE_ORACLE";
  }
  return "UNKNOWN";
}

RankDiagnostics evaluate_frozen_rank_guard(const CameraSystem &system) {
  RankDiagnostics diagnostics;
  diagnostics.guard_status = "nonfinite";
  diagnostics.guard_stage = "input_dimensions";
  if (!valid_system_dimensions(system)) {
    return diagnostics;
  }
  if (system.H_f.rows() <= 3) {
    diagnostics.guard_status = "insufficient_rows";
    diagnostics.guard_stage = "insufficient_rows";
    return diagnostics;
  }
  if (!system.H_x.allFinite() || !system.H_f.allFinite() ||
      !system.residual.allFinite() || !system.P_active.allFinite()) {
    diagnostics.guard_stage = "raw_inputs";
    return diagnostics;
  }
  if (!std::isfinite(system.sigma_px) || !(system.sigma_px > 0.0)) {
    diagnostics.guard_stage = "sigma";
    return diagnostics;
  }
  const Eigen::MatrixXd B = system.H_f.array() / system.sigma_px;
  if (!B.allFinite()) {
    diagnostics.guard_stage = "whitening";
    return diagnostics;
  }
  Eigen::JacobiSVD<Eigen::MatrixXd> svd(B, Eigen::ComputeThinU | Eigen::ComputeThinV);
  if (svd.singularValues().size() != 3 || !svd.singularValues().allFinite()) {
    diagnostics.guard_stage = "svd_spectrum";
    return diagnostics;
  }
  diagnostics.valid = true;
  diagnostics.singular_values_available = true;
  diagnostics.singular_values = svd.singularValues();
  const double largest = diagnostics.singular_values(0);
  const double smallest = diagnostics.singular_values(2);
  if (!(largest > std::numeric_limits<double>::min())) {
    diagnostics.guard_status = "rank_deficient";
    diagnostics.guard_stage = "largest_singular_value";
    return diagnostics;
  }
  diagnostics.numerical_rank_floor =
      static_cast<double>(std::max<Eigen::Index>(B.rows(), B.cols())) *
      std::numeric_limits<double>::epsilon() * largest;
  diagnostics.singular_ratio = smallest / largest;
  diagnostics.singular_ratio_available = true;
  diagnostics.numerical_rank = 0;
  for (Eigen::Index index = 0; index < diagnostics.singular_values.rows(); ++index) {
    if (diagnostics.singular_values(index) > diagnostics.numerical_rank_floor) {
      ++diagnostics.numerical_rank;
    }
  }
  // Frozen semantics: equality at the rank floor rejects.
  if (!(smallest > diagnostics.numerical_rank_floor)) {
    diagnostics.guard_status = "rank_deficient";
    diagnostics.guard_stage = "numerical_rank";
    return diagnostics;
  }
  // Frozen semantics: equality at the condition boundary accepts.
  if (diagnostics.singular_ratio < kFrozenMinimumSingularRatio) {
    diagnostics.guard_status = "ill_conditioned";
    diagnostics.guard_stage = "conditioning";
    return diagnostics;
  }
  diagnostics.frozen_guard_accepts = true;
  diagnostics.guard_status = "accepted";
  diagnostics.guard_stage = "accepted";
  return diagnostics;
}

MethodResult evaluate_method(const CameraSystem &system, Method method,
                             const MethodResult *reference) {
  const PriorSupport support = factor_prior_support(system.P_active);
  MethodResult result;
  switch (method) {
  case Method::kUpstreamNullspace:
  case Method::kLocalNullspace:
    result = production_nullspace(system, method, support);
    break;
  case Method::kGuardedNullspaceDrop:
    result = guarded_nullspace(system, support);
    break;
  case Method::kRankAwareNullspace:
    result = rank_aware_nullspace(system, method, support);
    break;
  case Method::kLocalSchur:
    result = local_schur(system, support);
    break;
  case Method::kFullJointOracle:
    result = full_joint_oracle(system, support);
    break;
  case Method::kFullUNullspaceOracle:
    result = rank_aware_nullspace(system, method, support);
    break;
  }
  if (!support.valid && result.status.empty()) {
    result.status = "rejected";
    result.reason = "prior_not_psd_on_supported_subspace";
  }
  if (reference != nullptr) {
    compare_with_reference(result, *reference);
  } else if ((method == Method::kFullJointOracle ||
              method == Method::kFullUNullspaceOracle) &&
             result.accepted) {
    result.state_increment_relative_error = 0.0;
    result.posterior_covariance_relative_error = 0.0;
    result.nis_relative_error = 0.0;
    result.harmful_accepted = frozen_harmful_acceptance(result);
  }
  return result;
}

bool frozen_harmful_acceptance(const MethodResult &result) noexcept {
  if (!result.accepted) {
    return false;
  }
  return !result.finite ||
         result.state_increment_relative_error > kFrozenHarmfulRelativeThreshold ||
         result.posterior_covariance_relative_error >
             kFrozenHarmfulRelativeThreshold ||
         result.scaled_psd_failure ||
         result.nis_relative_error > kFrozenHarmfulRelativeThreshold ||
         result.unexplained_input_mutation;
}

Comparison evaluate_all(const CameraSystem &system) {
  Comparison comparison;
  const CameraSystem before = system;
  comparison.rank = evaluate_frozen_rank_guard(system);

  MethodResult full_u =
      evaluate_method(system, Method::kFullUNullspaceOracle, nullptr);
  MethodResult full_joint =
      evaluate_method(system, Method::kFullJointOracle, nullptr);
  MethodResult full_joint_vs_full_u = full_joint;
  compare_with_reference(full_joint_vs_full_u, full_u);
  // All reported method-output safety rows use FULL_JOINT as the common
  // reference. Keep FULL_JOINT's independent error versus FULL_U in a
  // separate value used only by the oracle-agreement decision.
  compare_with_reference(full_u, full_joint);

  const Method methods[] = {
      Method::kUpstreamNullspace,
      Method::kLocalNullspace,
      Method::kGuardedNullspaceDrop,
      Method::kRankAwareNullspace,
      Method::kLocalSchur,
  };
  comparison.methods.reserve(7);
  for (const Method method : methods) {
    comparison.methods.push_back(evaluate_method(system, method, &full_joint));
  }
  comparison.methods.push_back(std::move(full_joint));
  comparison.methods.push_back(full_u);

  comparison.oracle_agreement.full_u_valid = full_u.accepted && full_u.finite;
  const MethodResult &joint = comparison.methods[5];
  comparison.oracle_agreement.full_joint_valid = joint.accepted && joint.finite;
  comparison.oracle_agreement.state_increment_relative_error =
      full_joint_vs_full_u.state_increment_relative_error;
  comparison.oracle_agreement.posterior_covariance_relative_error =
      full_joint_vs_full_u.posterior_covariance_relative_error;
  comparison.oracle_agreement.full_u_scaled_psd_failure =
      full_u.scaled_psd_failure;
  comparison.oracle_agreement.full_joint_scaled_psd_failure =
      joint.scaled_psd_failure;
  comparison.oracle_agreement.oracle_safe_opportunity =
      comparison.oracle_agreement.full_u_valid &&
      comparison.oracle_agreement.full_joint_valid &&
      !comparison.oracle_agreement.full_u_scaled_psd_failure &&
      !comparison.oracle_agreement.full_joint_scaled_psd_failure &&
      comparison.oracle_agreement.state_increment_relative_error <=
          kFrozenHarmfulRelativeThreshold &&
      comparison.oracle_agreement.posterior_covariance_relative_error <=
          kFrozenHarmfulRelativeThreshold &&
      full_joint_vs_full_u.nis_relative_error <=
          kFrozenHarmfulRelativeThreshold;

  comparison.unguarded_nullspace_harmful = comparison.methods[0].harmful_accepted;
  comparison.caller_input_mutated =
      !bitwise_equal(system.H_x, before.H_x) ||
      !bitwise_equal(system.H_f, before.H_f) ||
      !bitwise_equal(system.residual, before.residual) ||
      !bitwise_equal(system.P_active, before.P_active) ||
      std::memcmp(&system.sigma_px, &before.sigma_px, sizeof(double)) != 0;
  return comparison;
}

} // namespace conditioning
} // namespace ov_msckf
