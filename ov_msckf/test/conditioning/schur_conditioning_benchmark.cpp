/*
 * Deterministic numerical-conditioning benchmark for SchurVIO-Lite.
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * This is a test/analysis harness. It does not alter production estimator
 * mathematics. See test/conditioning/README.md for the frozen protocol.
 */

#include "update/SchurUpdate.h"
#include "update/UpdaterHelper.h"

#include <Eigen/Cholesky>
#include <Eigen/Eigenvalues>
#include <Eigen/QR>
#include <Eigen/SVD>

#include <algorithm>
#include <array>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <limits>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

namespace {

constexpr std::uint64_t kMasterSeed = UINT64_C(0x434f4e444954494f);
constexpr int kTimingRepetitions = 9;
constexpr double kRelativeDenominatorFloor = 1.0e-12;
constexpr double kInformationParityRelative = 1.0e-8;
constexpr double kPosteriorParityRelative = 1.0e-7;
constexpr double kHarmfulRelative = 1.0e-3;
constexpr int kCameraCalibrationDimensions = 29;

constexpr const char *kLocalSchurSourceSha256 =
    "2237619f8dd81054a9722967f356766182980e4e20f86a4145002aff7aa66c03";
constexpr const char *kOpenVinsNullspaceSourceSha256 =
    "f68fc2e1dd04a6a33e94c576839dc4643114d2100ddb69f1b00da09bd7b12714";
constexpr const char *kOpenVinsNullspaceGitBlob = "b36c004f78093b48d483bccef38726eb4f7c7810";

volatile double g_timing_sink = 0.0;

class SplitMix64 {
public:
  explicit SplitMix64(std::uint64_t seed) : state_(seed) {}

  std::uint64_t next_u64() {
    std::uint64_t z = (state_ += UINT64_C(0x9e3779b97f4a7c15));
    z = (z ^ (z >> 30U)) * UINT64_C(0xbf58476d1ce4e5b9);
    z = (z ^ (z >> 27U)) * UINT64_C(0x94d049bb133111eb);
    return z ^ (z >> 31U);
  }

  double uniform01() {
    const std::uint64_t top53 = next_u64() >> 11U;
    return static_cast<double>(top53) * (1.0 / 9007199254740992.0);
  }

  double symmetric() { return 2.0 * uniform01() - 1.0; }

  Eigen::MatrixXd matrix(Eigen::Index rows, Eigen::Index cols, double scale = 1.0) {
    Eigen::MatrixXd value(rows, cols);
    for (Eigen::Index row = 0; row < rows; ++row) {
      for (Eigen::Index col = 0; col < cols; ++col) {
        value(row, col) = scale * symmetric();
      }
    }
    return value;
  }

  Eigen::VectorXd vector(Eigen::Index rows, double scale = 1.0) {
    Eigen::VectorXd value(rows);
    for (Eigen::Index row = 0; row < rows; ++row) {
      value(row) = scale * symmetric();
    }
    return value;
  }

private:
  std::uint64_t state_;
};

struct SpectrumSpec {
  std::string name;
  double target_condition = std::numeric_limits<double>::quiet_NaN();
  int exact_rank = 3;
  Eigen::Vector3d singular_values = Eigen::Vector3d::Ones();
};

struct LayoutSpec {
  std::string name;
  int state_dimension = 0;
  bool has_camera_calibration_block = false;
};

struct Fixture {
  int id = 0;
  std::uint64_t seed = 0;
  int rows = 0;
  LayoutSpec layout;
  SpectrumSpec spectrum;
  std::string geometry;
  bool calibration_active = false;
  std::string noise_model = "isotropic_scalar";
  Eigen::MatrixXd H_x;
  Eigen::MatrixXd H_f;
  Eigen::VectorXd residual;
  Eigen::MatrixXd prior_covariance;
};

struct LinearSystem {
  Eigen::MatrixXd H_x;
  Eigen::MatrixXd H_f;
  Eigen::VectorXd residual;
};

struct PosteriorResult {
  bool valid = false;
  Eigen::VectorXd dx;
  Eigen::MatrixXd covariance;
  double nis = std::numeric_limits<double>::quiet_NaN();
  double objective_cost = std::numeric_limits<double>::quiet_NaN();
  double covariance_symmetry_error_inf = std::numeric_limits<double>::quiet_NaN();
  double minimum_covariance_eigenvalue = std::numeric_limits<double>::quiet_NaN();
  double psd_tolerance = std::numeric_limits<double>::quiet_NaN();
};

struct OracleResult {
  bool valid = false;
  int numerical_rank = 0;
  double condition_number = std::numeric_limits<double>::infinity();
  Eigen::MatrixXd lambda;
  Eigen::VectorXd eta;
  double gamma = std::numeric_limits<double>::quiet_NaN();
  PosteriorResult covariance_form;
  PosteriorResult information_form;
  Eigen::VectorXd full_joint_dx;
  Eigen::MatrixXd full_joint_covariance;
  double full_joint_dx_relative_error = std::numeric_limits<double>::quiet_NaN();
  double full_joint_covariance_relative_error = std::numeric_limits<double>::quiet_NaN();
  double pseudoinverse_information_relative_error = std::numeric_limits<double>::quiet_NaN();
  double pseudoinverse_gradient_relative_error = std::numeric_limits<double>::quiet_NaN();
  double information_covariance_dx_relative_error = std::numeric_limits<double>::quiet_NaN();
  double information_covariance_covariance_relative_error = std::numeric_limits<double>::quiet_NaN();
};

struct ReductionOutput {
  bool accepted = false;
  std::string status;
  std::string reason;
  Eigen::MatrixXd H_reduced;
  Eigen::VectorXd residual_reduced;
  Eigen::MatrixXd lambda;
  Eigen::VectorXd eta;
  double gamma = std::numeric_limits<double>::quiet_NaN();
  bool input_mutation = false;
  bool mutation_expected = false;
  double runtime_ns = std::numeric_limits<double>::quiet_NaN();
};

struct MethodRow {
  std::string method;
  std::string implementation_label;
  std::string source_identity;
  ReductionOutput reduction;
  PosteriorResult posterior;
  double reduced_information_relative_error = std::numeric_limits<double>::quiet_NaN();
  double reduced_gradient_relative_error = std::numeric_limits<double>::quiet_NaN();
  double state_increment_relative_error = std::numeric_limits<double>::quiet_NaN();
  double residual_cost_relative_error = std::numeric_limits<double>::quiet_NaN();
  double nis_relative_error = std::numeric_limits<double>::quiet_NaN();
  double posterior_covariance_relative_error = std::numeric_limits<double>::quiet_NaN();
  bool finite = false;
  bool unexplained_input_mutation = false;
  bool harmful_accepted = false;
  bool safe_rejection = false;
  bool well_conditioned_parity = false;
};

double matrix_inf_norm(const Eigen::MatrixXd &value) {
  if (value.size() == 0) {
    return 0.0;
  }
  return value.cwiseAbs().rowwise().sum().maxCoeff();
}

double relative_error(const Eigen::MatrixXd &value, const Eigen::MatrixXd &reference) {
  if (value.rows() != reference.rows() || value.cols() != reference.cols() || !value.allFinite() || !reference.allFinite()) {
    return std::numeric_limits<double>::infinity();
  }
  return (value - reference).norm() / std::max(kRelativeDenominatorFloor, reference.norm());
}

double relative_error(const Eigen::VectorXd &value, const Eigen::VectorXd &reference) {
  if (value.rows() != reference.rows() || !value.allFinite() || !reference.allFinite()) {
    return std::numeric_limits<double>::infinity();
  }
  return (value - reference).norm() / std::max(kRelativeDenominatorFloor, reference.norm());
}

double relative_error(double value, double reference) {
  if (!std::isfinite(value) || !std::isfinite(reference)) {
    return std::numeric_limits<double>::infinity();
  }
  return std::abs(value - reference) / std::max(kRelativeDenominatorFloor, std::abs(reference));
}

bool bitwise_equal(const Eigen::MatrixXd &left, const Eigen::MatrixXd &right) {
  return left.rows() == right.rows() && left.cols() == right.cols() &&
         std::memcmp(left.data(), right.data(), static_cast<std::size_t>(left.size()) * sizeof(double)) == 0;
}

bool bitwise_equal(const Eigen::VectorXd &left, const Eigen::VectorXd &right) {
  return left.rows() == right.rows() &&
         std::memcmp(left.data(), right.data(), static_cast<std::size_t>(left.size()) * sizeof(double)) == 0;
}

Eigen::MatrixXd orthonormal_columns(Eigen::MatrixXd seed, Eigen::Index columns) {
  Eigen::HouseholderQR<Eigen::MatrixXd> qr(seed);
  return qr.householderQ() * Eigen::MatrixXd::Identity(seed.rows(), columns);
}

Eigen::MatrixXd make_left_basis(int rows, const std::string &geometry, SplitMix64 &rng) {
  Eigen::MatrixXd seed(rows, 3);
  if (geometry == "very_low_parallax") {
    for (int row = 0; row < rows; ++row) {
      const double t = rows == 1 ? 0.0 : -1.0 + 2.0 * static_cast<double>(row) / static_cast<double>(rows - 1);
      seed(row, 0) = 1.0 + 1.0e-4 * rng.symmetric();
      seed(row, 1) = 1.0e-3 * t + 1.0e-5 * rng.symmetric();
      seed(row, 2) = 1.0e-6 * t * t + 1.0e-7 * rng.symmetric();
    }
  } else if (geometry == "near_collinear_rays") {
    for (int row = 0; row < rows; ++row) {
      const double t = static_cast<double>(row + 1) / static_cast<double>(rows);
      const double base = 1.0 + 0.1 * t;
      seed(row, 0) = base;
      seed(row, 1) = base + 1.0e-5 * rng.symmetric();
      seed(row, 2) = base - 1.0e-5 * rng.symmetric();
    }
  } else if (geometry == "repeated_view_geometry") {
    Eigen::Vector3d previous = Eigen::Vector3d::Zero();
    for (int row = 0; row < rows; ++row) {
      if (row % 2 == 0) {
        previous = rng.vector(3, 1.0);
      }
      seed.row(row) = previous.transpose();
      if (row % 2 == 1) {
        seed(row, row % 3) += 1.0e-9;
      }
    }
  } else {
    seed = rng.matrix(rows, 3, 1.0);
  }
  return orthonormal_columns(seed, 3);
}

Eigen::Matrix3d make_right_basis(const std::string &geometry, SplitMix64 &rng) {
  Eigen::Matrix3d seed;
  if (geometry == "near_collinear_rays") {
    seed << 1.0, 1.0, 1.0, 1.0, 1.0 + 1.0e-6, 1.0 - 1.0e-6, 1.0, 1.0 - 1.0e-6, 1.0 + 2.0e-6;
  } else {
    seed = rng.matrix(3, 3, 1.0);
  }
  return orthonormal_columns(seed, 3);
}

Eigen::MatrixXd make_prior_covariance(int dimension, SplitMix64 &rng) {
  Eigen::VectorXd diagonal(dimension);
  for (int index = 0; index < dimension; ++index) {
    diagonal(index) = 0.02 + 0.003 * static_cast<double>(index % 11);
  }
  const Eigen::MatrixXd coupling = rng.matrix(dimension, 6, 0.015);
  Eigen::MatrixXd covariance = diagonal.asDiagonal();
  covariance.noalias() += coupling * coupling.transpose();
  return 0.5 * (covariance + covariance.transpose());
}

Fixture make_fixture(int id, int rows, const LayoutSpec &layout, const SpectrumSpec &spectrum,
                     const std::string &geometry) {
  Fixture fixture;
  fixture.id = id;
  fixture.seed = kMasterSeed ^ (static_cast<std::uint64_t>(id + 1) * UINT64_C(0x9e3779b97f4a7c15));
  fixture.rows = rows;
  fixture.layout = layout;
  fixture.spectrum = spectrum;
  fixture.geometry = geometry;
  fixture.calibration_active = layout.has_camera_calibration_block && geometry != "calibration_inactive";
  SplitMix64 rng(fixture.seed);

  const Eigen::MatrixXd left = make_left_basis(rows, geometry, rng);
  const Eigen::Matrix3d right = make_right_basis(geometry, rng);
  const double depth_scale = geometry == "far_depth" ? 1.0e-6 : 1.0;
  fixture.H_f = depth_scale * left * spectrum.singular_values.asDiagonal() * right.transpose();

  fixture.H_x = rng.matrix(rows, layout.state_dimension, 0.15);
  const double contamination_scale =
      (geometry == "very_low_parallax" || geometry == "near_collinear_rays" || geometry == "repeated_view_geometry") ? 20.0 : 5.0;
  fixture.H_x.noalias() += contamination_scale * fixture.H_f * rng.matrix(3, layout.state_dimension, 0.4);
  if (layout.has_camera_calibration_block && !fixture.calibration_active) {
    fixture.H_x.rightCols(kCameraCalibrationDimensions).setZero();
  }

  const Eigen::VectorXd state_truth = rng.vector(layout.state_dimension, 0.02 / std::sqrt(layout.state_dimension));
  const Eigen::Vector3d landmark_truth = rng.vector(3, 0.5);
  fixture.residual = fixture.H_x * state_truth + fixture.H_f * landmark_truth + rng.vector(rows, 0.01);
  if (geometry == "controlled_visual_outlier") {
    fixture.residual(0) += 25.0;
    fixture.residual(1) -= 20.0;
  }

  if (geometry == "anisotropic_image_noise") {
    fixture.noise_model = "prewhitened_anisotropic_0.25_1_4_2";
    const std::array<double, 4> sigmas{{0.25, 1.0, 4.0, 2.0}};
    // Construct raw anisotropic rows and explicitly prewhiten them. The
    // consumed system retains the declared singular spectrum up to roundoff.
    for (int row = 0; row < rows; ++row) {
      const double sigma = sigmas[static_cast<std::size_t>(row) % sigmas.size()];
      const Eigen::RowVectorXd raw_hx = sigma * fixture.H_x.row(row);
      const Eigen::RowVector3d raw_hf = sigma * fixture.H_f.row(row);
      const double raw_residual = sigma * fixture.residual(row);
      fixture.H_x.row(row) = raw_hx / sigma;
      fixture.H_f.row(row) = raw_hf / sigma;
      fixture.residual(row) = raw_residual / sigma;
    }
  }

  fixture.prior_covariance = make_prior_covariance(layout.state_dimension, rng);
  return fixture;
}

double minimized_objective(const LinearSystem &system, const Eigen::MatrixXd &prior_inverse,
                           const Eigen::VectorXd &dx) {
  if (!system.H_x.allFinite() || !system.H_f.allFinite() || !system.residual.allFinite() || !prior_inverse.allFinite() ||
      !dx.allFinite()) {
    return std::numeric_limits<double>::quiet_NaN();
  }
  Eigen::CompleteOrthogonalDecomposition<Eigen::MatrixXd> cod(system.H_f);
  const double scale = system.H_f.norm();
  cod.setThreshold(std::max(system.H_f.rows(), system.H_f.cols()) * std::numeric_limits<double>::epsilon() *
                   std::max(1.0, scale));
  const Eigen::VectorXd landmark = cod.solve(system.residual - system.H_x * dx);
  const Eigen::VectorXd remaining = system.residual - system.H_x * dx - system.H_f * landmark;
  return 0.5 * (dx.dot(prior_inverse * dx) + remaining.squaredNorm());
}

void finalize_covariance_diagnostics(PosteriorResult &result) {
  if (!result.covariance.allFinite()) {
    result.valid = false;
    return;
  }
  result.covariance_symmetry_error_inf = matrix_inf_norm(result.covariance - result.covariance.transpose());
  result.covariance = 0.5 * (result.covariance + result.covariance.transpose());
  Eigen::SelfAdjointEigenSolver<Eigen::MatrixXd> eigen_solver(result.covariance);
  if (eigen_solver.info() != Eigen::Success || !eigen_solver.eigenvalues().allFinite()) {
    result.valid = false;
    return;
  }
  result.minimum_covariance_eigenvalue = eigen_solver.eigenvalues().minCoeff();
  const double max_abs_eigenvalue = eigen_solver.eigenvalues().cwiseAbs().maxCoeff();
  result.psd_tolerance = 256.0 * static_cast<double>(result.covariance.rows()) * std::numeric_limits<double>::epsilon() *
                         std::max(1.0, max_abs_eigenvalue);
}

PosteriorResult posterior_from_rows(const Eigen::MatrixXd &H, const Eigen::VectorXd &residual,
                                    const Eigen::MatrixXd &prior_covariance, const LinearSystem &cost_system,
                                    const Eigen::MatrixXd &prior_inverse) {
  PosteriorResult result;
  if (H.rows() != residual.rows() || H.cols() != prior_covariance.rows() || !H.allFinite() || !residual.allFinite() ||
      !prior_covariance.allFinite()) {
    return result;
  }
  const Eigen::MatrixXd M = prior_covariance * H.transpose();
  Eigen::MatrixXd innovation = H * M;
  innovation.diagonal().array() += 1.0;
  Eigen::LLT<Eigen::MatrixXd> innovation_factor(innovation.selfadjointView<Eigen::Upper>());
  if (innovation_factor.info() != Eigen::Success) {
    return result;
  }
  const Eigen::VectorXd solved_residual = innovation_factor.solve(residual);
  const Eigen::MatrixXd solved_m_transpose = innovation_factor.solve(M.transpose());
  if (innovation_factor.info() != Eigen::Success || !solved_residual.allFinite() || !solved_m_transpose.allFinite()) {
    return result;
  }
  result.dx = M * solved_residual;
  result.nis = residual.dot(solved_residual);
  result.covariance = prior_covariance - M * solved_m_transpose;
  result.objective_cost = minimized_objective(cost_system, prior_inverse, result.dx);
  result.valid = result.dx.allFinite() && std::isfinite(result.nis) && std::isfinite(result.objective_cost);
  finalize_covariance_diagnostics(result);
  return result;
}

PosteriorResult posterior_from_information(const Eigen::MatrixXd &lambda, const Eigen::VectorXd &eta,
                                           const Eigen::MatrixXd &prior_covariance, const LinearSystem &cost_system,
                                           const Eigen::MatrixXd &prior_inverse) {
  PosteriorResult result;
  if (lambda.rows() != lambda.cols() || lambda.rows() != eta.rows() || lambda.rows() != prior_covariance.rows() ||
      !lambda.allFinite() || !eta.allFinite()) {
    return result;
  }
  Eigen::MatrixXd information = prior_inverse + 0.5 * (lambda + lambda.transpose());
  Eigen::LDLT<Eigen::MatrixXd> factor(information.selfadjointView<Eigen::Upper>());
  if (factor.info() != Eigen::Success) {
    return result;
  }
  result.dx = factor.solve(eta);
  result.covariance = factor.solve(Eigen::MatrixXd::Identity(information.rows(), information.cols()));
  if (factor.info() != Eigen::Success || !result.dx.allFinite() || !result.covariance.allFinite()) {
    return result;
  }
  result.objective_cost = minimized_objective(cost_system, prior_inverse, result.dx);
  result.valid = std::isfinite(result.objective_cost);
  finalize_covariance_diagnostics(result);
  return result;
}

OracleResult compute_oracle(const LinearSystem &system, const Eigen::MatrixXd &prior_covariance) {
  OracleResult oracle;
  if (system.H_f.cols() != 3 || system.H_x.rows() != system.H_f.rows() || system.residual.rows() != system.H_f.rows() ||
      system.H_x.cols() != prior_covariance.rows() || !system.H_x.allFinite() || !system.H_f.allFinite() ||
      !system.residual.allFinite() || !prior_covariance.allFinite()) {
    return oracle;
  }

  Eigen::LDLT<Eigen::MatrixXd> prior_factor(prior_covariance.selfadjointView<Eigen::Upper>());
  if (prior_factor.info() != Eigen::Success) {
    return oracle;
  }
  const Eigen::MatrixXd prior_inverse =
      prior_factor.solve(Eigen::MatrixXd::Identity(prior_covariance.rows(), prior_covariance.cols()));
  if (prior_factor.info() != Eigen::Success || !prior_inverse.allFinite()) {
    return oracle;
  }

  Eigen::JacobiSVD<Eigen::MatrixXd> feature_svd(system.H_f, Eigen::ComputeFullU | Eigen::ComputeFullV);
  if (feature_svd.singularValues().size() != 3 || !feature_svd.singularValues().allFinite()) {
    return oracle;
  }
  const double largest = feature_svd.singularValues()(0);
  const double rank_floor = static_cast<double>(std::max(system.H_f.rows(), system.H_f.cols())) *
                            std::numeric_limits<double>::epsilon() * largest;
  oracle.numerical_rank = 0;
  for (Eigen::Index index = 0; index < feature_svd.singularValues().rows(); ++index) {
    if (feature_svd.singularValues()(index) > rank_floor) {
      ++oracle.numerical_rank;
    }
  }
  const double smallest = feature_svd.singularValues()(2);
  oracle.condition_number = smallest > rank_floor ? largest / smallest : std::numeric_limits<double>::infinity();

  const int nullity = system.H_f.rows() - oracle.numerical_rank;
  if (nullity <= 0) {
    return oracle;
  }
  const Eigen::MatrixXd nullspace_transpose =
      feature_svd.matrixU().rightCols(nullity).transpose();
  const Eigen::MatrixXd H_null = nullspace_transpose * system.H_x;
  const Eigen::VectorXd residual_null = nullspace_transpose * system.residual;
  oracle.lambda = H_null.transpose() * H_null;
  oracle.lambda = 0.5 * (oracle.lambda + oracle.lambda.transpose());
  oracle.eta = H_null.transpose() * residual_null;
  oracle.gamma = residual_null.squaredNorm();
  oracle.covariance_form = posterior_from_rows(H_null, residual_null, prior_covariance, system, prior_inverse);
  oracle.information_form = posterior_from_information(oracle.lambda, oracle.eta, prior_covariance, system, prior_inverse);

  // Independent Moore-Penrose projector via complete orthogonal decomposition.
  Eigen::CompleteOrthogonalDecomposition<Eigen::MatrixXd> cod(system.H_f);
  cod.setThreshold(rank_floor / std::max(largest, std::numeric_limits<double>::min()));
  const Eigen::MatrixXd feature_pseudoinverse =
      cod.solve(Eigen::MatrixXd::Identity(system.H_f.rows(), system.H_f.rows()));
  const Eigen::MatrixXd projector =
      Eigen::MatrixXd::Identity(system.H_f.rows(), system.H_f.rows()) - system.H_f * feature_pseudoinverse;
  const Eigen::MatrixXd lambda_pseudoinverse = system.H_x.transpose() * projector * system.H_x;
  const Eigen::VectorXd eta_pseudoinverse = system.H_x.transpose() * projector * system.residual;
  oracle.pseudoinverse_information_relative_error = relative_error(lambda_pseudoinverse, oracle.lambda);
  oracle.pseudoinverse_gradient_relative_error = relative_error(eta_pseudoinverse, oracle.eta);

  oracle.information_covariance_dx_relative_error =
      relative_error(oracle.information_form.dx, oracle.covariance_form.dx);
  oracle.information_covariance_covariance_relative_error =
      relative_error(oracle.information_form.covariance, oracle.covariance_form.covariance);

  // Independent full joint prior/measurement least-squares SVD.
  Eigen::LLT<Eigen::MatrixXd> prior_llt(prior_covariance.selfadjointView<Eigen::Upper>());
  if (prior_llt.info() != Eigen::Success) {
    return oracle;
  }
  const Eigen::MatrixXd prior_sqrt_information =
      prior_llt.matrixL().solve(Eigen::MatrixXd::Identity(prior_covariance.rows(), prior_covariance.cols()));
  const int state_dimension = prior_covariance.rows();
  Eigen::MatrixXd joint = Eigen::MatrixXd::Zero(state_dimension + system.H_x.rows(), state_dimension + 3);
  joint.topLeftCorner(state_dimension, state_dimension) = prior_sqrt_information;
  joint.block(state_dimension, 0, system.H_x.rows(), state_dimension) = system.H_x;
  joint.block(state_dimension, state_dimension, system.H_f.rows(), 3) = system.H_f;
  Eigen::VectorXd joint_rhs = Eigen::VectorXd::Zero(state_dimension + system.H_x.rows());
  joint_rhs.tail(system.residual.rows()) = system.residual;
  Eigen::JacobiSVD<Eigen::MatrixXd> joint_svd(joint, Eigen::ComputeThinU | Eigen::ComputeThinV);
  if (!joint_svd.singularValues().allFinite()) {
    return oracle;
  }
  const double joint_floor = static_cast<double>(std::max(joint.rows(), joint.cols())) *
                             std::numeric_limits<double>::epsilon() * joint_svd.singularValues()(0);
  Eigen::VectorXd joint_coefficients = joint_svd.matrixU().transpose() * joint_rhs;
  Eigen::VectorXd inverse_squared = Eigen::VectorXd::Zero(joint_svd.singularValues().rows());
  for (Eigen::Index index = 0; index < joint_svd.singularValues().rows(); ++index) {
    const double singular_value = joint_svd.singularValues()(index);
    if (singular_value > joint_floor) {
      joint_coefficients(index) /= singular_value;
      inverse_squared(index) = 1.0 / (singular_value * singular_value);
    } else {
      joint_coefficients(index) = 0.0;
    }
  }
  const Eigen::VectorXd joint_solution = joint_svd.matrixV() * joint_coefficients;
  const Eigen::MatrixXd joint_covariance =
      joint_svd.matrixV() * inverse_squared.asDiagonal() * joint_svd.matrixV().transpose();
  oracle.full_joint_dx = joint_solution.head(state_dimension);
  oracle.full_joint_covariance = joint_covariance.topLeftCorner(state_dimension, state_dimension);
  oracle.full_joint_dx_relative_error = relative_error(oracle.full_joint_dx, oracle.covariance_form.dx);
  oracle.full_joint_covariance_relative_error =
      relative_error(oracle.full_joint_covariance, oracle.covariance_form.covariance);

  oracle.valid = oracle.lambda.allFinite() && oracle.eta.allFinite() && std::isfinite(oracle.gamma) &&
                 oracle.covariance_form.valid && oracle.information_form.valid && oracle.full_joint_dx.allFinite() &&
                 oracle.full_joint_covariance.allFinite() && std::isfinite(oracle.pseudoinverse_information_relative_error) &&
                 std::isfinite(oracle.pseudoinverse_gradient_relative_error);
  return oracle;
}

double median_runtime(std::vector<double> samples) {
  std::sort(samples.begin(), samples.end());
  return samples[samples.size() / 2];
}

ReductionOutput run_local_schur(const Fixture &fixture) {
  ReductionOutput output;
  const Eigen::MatrixXd H_x_before = fixture.H_x;
  const Eigen::MatrixXd H_f_before = fixture.H_f;
  const Eigen::VectorXd residual_before = fixture.residual;
  const ov_msckf::SchurReductionResult result =
      ov_msckf::SchurUpdate::Reduce(fixture.H_x, fixture.H_f, fixture.residual, 1.0);
  output.accepted = result.accepted();
  output.status = ov_msckf::schur_reduction_status_name(result.status);
  output.reason = ov_msckf::schur_reduction_stage_name(result.stage);
  output.input_mutation = !bitwise_equal(fixture.H_x, H_x_before) || !bitwise_equal(fixture.H_f, H_f_before) ||
                          !bitwise_equal(fixture.residual, residual_before);
  output.mutation_expected = false;
  if (result.accepted()) {
    output.H_reduced = result.H_reduced;
    output.residual_reduced = result.residual_reduced;
    output.lambda = result.lambda;
    output.eta = result.eta;
    output.gamma = result.gamma;
  }
  std::vector<double> samples;
  samples.reserve(kTimingRepetitions);
  for (int repetition = 0; repetition < kTimingRepetitions; ++repetition) {
    const auto start = std::chrono::steady_clock::now();
    const ov_msckf::SchurReductionResult timed =
        ov_msckf::SchurUpdate::Reduce(fixture.H_x, fixture.H_f, fixture.residual, 1.0);
    const auto stop = std::chrono::steady_clock::now();
    samples.push_back(std::chrono::duration<double, std::nano>(stop - start).count());
    g_timing_sink += static_cast<double>(timed.raw_rows) + static_cast<double>(timed.degrees_of_freedom);
  }
  output.runtime_ns = median_runtime(samples);
  return output;
}

ReductionOutput run_production_nullspace(const Fixture &fixture) {
  ReductionOutput output;
  Eigen::MatrixXd H_x = fixture.H_x;
  Eigen::MatrixXd H_f = fixture.H_f;
  Eigen::VectorXd residual = fixture.residual;
  ov_msckf::UpdaterHelper::nullspace_project_inplace(H_f, H_x, residual);
  output.accepted = true; // production helper has no status/rank return
  output.status = "accepted_no_rank_guard";
  output.reason = "production_inplace_givens";
  output.H_reduced = H_x;
  output.residual_reduced = residual;
  output.lambda = H_x.transpose() * H_x;
  output.lambda = 0.5 * (output.lambda + output.lambda.transpose());
  output.eta = H_x.transpose() * residual;
  output.gamma = residual.squaredNorm();
  output.input_mutation = !bitwise_equal(H_x, fixture.H_x) || !bitwise_equal(H_f, fixture.H_f) ||
                          !bitwise_equal(residual, fixture.residual);
  output.mutation_expected = true;

  std::vector<double> samples;
  samples.reserve(kTimingRepetitions);
  for (int repetition = 0; repetition < kTimingRepetitions; ++repetition) {
    Eigen::MatrixXd timed_H_x = fixture.H_x;
    Eigen::MatrixXd timed_H_f = fixture.H_f;
    Eigen::VectorXd timed_residual = fixture.residual;
    const auto start = std::chrono::steady_clock::now();
    ov_msckf::UpdaterHelper::nullspace_project_inplace(timed_H_f, timed_H_x, timed_residual);
    const auto stop = std::chrono::steady_clock::now();
    samples.push_back(std::chrono::duration<double, std::nano>(stop - start).count());
    g_timing_sink += timed_H_x.size() == 0 ? 0.0 : timed_H_x(0, 0);
  }
  output.runtime_ns = median_runtime(samples);
  return output;
}

MethodRow evaluate_method(const Fixture &fixture, const LinearSystem &base_system, const OracleResult &base_oracle,
                          const std::string &method) {
  MethodRow row;
  row.method = method;
  const OracleResult *reference = &base_oracle;
  const LinearSystem *cost_system = &base_system;
  if (method == "L_SCHUR") {
    row.implementation_label = "actual_local_SchurUpdate_Reduce";
    row.source_identity = kLocalSchurSourceSha256;
    row.reduction = run_local_schur(fixture);
  } else if (method == "L_NS") {
    row.implementation_label = "actual_local_production_nullspace";
    row.source_identity = kOpenVinsNullspaceSourceSha256;
    row.reduction = run_production_nullspace(fixture);
  } else if (method == "U_NS") {
    row.implementation_label = "exact_upstream_source_identical_alias";
    row.source_identity = std::string(kOpenVinsNullspaceSourceSha256) + ":blob=" + kOpenVinsNullspaceGitBlob;
    row.reduction = run_production_nullspace(fixture);
  } else {
    throw std::runtime_error("unknown method " + method);
  }

  Eigen::LDLT<Eigen::MatrixXd> prior_factor(fixture.prior_covariance.selfadjointView<Eigen::Upper>());
  const Eigen::MatrixXd prior_inverse =
      prior_factor.solve(Eigen::MatrixXd::Identity(fixture.layout.state_dimension, fixture.layout.state_dimension));
  if (row.reduction.accepted) {
    row.posterior = posterior_from_rows(row.reduction.H_reduced, row.reduction.residual_reduced,
                                       fixture.prior_covariance, *cost_system, prior_inverse);
    row.reduced_information_relative_error = relative_error(row.reduction.lambda, reference->lambda);
    row.reduced_gradient_relative_error = relative_error(row.reduction.eta, reference->eta);
    row.state_increment_relative_error = relative_error(row.posterior.dx, reference->covariance_form.dx);
    row.posterior_covariance_relative_error =
        relative_error(row.posterior.covariance, reference->covariance_form.covariance);
    row.nis_relative_error = relative_error(row.posterior.nis, reference->covariance_form.nis);
    row.residual_cost_relative_error =
        relative_error(row.posterior.objective_cost, reference->covariance_form.objective_cost);
    row.finite = row.reduction.lambda.allFinite() && row.reduction.eta.allFinite() && row.posterior.valid &&
                 row.posterior.dx.allFinite() && row.posterior.covariance.allFinite() &&
                 std::isfinite(row.posterior.nis) && std::isfinite(row.posterior.objective_cost);
  }

  row.unexplained_input_mutation = row.reduction.input_mutation && !row.reduction.mutation_expected;
  const bool psd_failure = row.posterior.valid &&
                           row.posterior.minimum_covariance_eigenvalue < -row.posterior.psd_tolerance;
  row.harmful_accepted =
      row.reduction.accepted &&
      (!row.finite || row.state_increment_relative_error > kHarmfulRelative ||
       row.posterior_covariance_relative_error > kHarmfulRelative || row.nis_relative_error > kHarmfulRelative ||
       psd_failure || row.unexplained_input_mutation);
  row.safe_rejection = !row.reduction.accepted && reference->valid;

  const bool well_conditioned = reference->numerical_rank == 3 &&
                                std::isfinite(reference->condition_number) && reference->condition_number <= 1.0e4;
  row.well_conditioned_parity =
      well_conditioned && row.reduction.accepted && row.finite &&
      row.reduced_information_relative_error <= kInformationParityRelative &&
      row.reduced_gradient_relative_error <= kInformationParityRelative &&
      row.state_increment_relative_error <= kPosteriorParityRelative &&
      row.posterior_covariance_relative_error <= kPosteriorParityRelative &&
      row.nis_relative_error <= kPosteriorParityRelative && !psd_failure && !row.unexplained_input_mutation;
  return row;
}

std::string csv_escape(const std::string &value) {
  if (value.find_first_of(",\"\n\r") == std::string::npos) {
    return value;
  }
  std::string escaped = "\"";
  for (const char character : value) {
    if (character == '\"') {
      escaped += "\"\"";
    } else {
      escaped += character;
    }
  }
  escaped += "\"";
  return escaped;
}

void write_double(std::ostream &stream, double value) {
  if (std::isnan(value)) {
    stream << "nan";
  } else if (std::isinf(value)) {
    stream << (value > 0.0 ? "inf" : "-inf");
  } else {
    stream << std::setprecision(17) << value;
  }
}

void write_header(std::ostream &stream) {
  stream << "fixture_id,master_seed,fixture_seed,method,implementation_label,source_identity,rows,state_dimension,"
            "state_layout,geometry_family,target_spectrum,target_condition,target_exact_rank,measured_condition,"
            "numerical_rank,calibration_active,fej_enabled,noise_model,accepted,status,rejection_reason,finite,"
            "reduced_information_relative_error,reduced_gradient_relative_error,state_increment_relative_error,"
            "residual_cost_relative_error,nis_relative_error,posterior_covariance_relative_error,"
            "covariance_symmetry_error_inf,minimum_covariance_eigenvalue,psd_tolerance,input_mutation,"
            "mutation_expected,unexplained_input_mutation,harmful_accepted,safe_rejection,well_conditioned_parity,"
            "runtime_ns,oracle_full_joint_dx_relative_error,oracle_full_joint_covariance_relative_error,"
            "oracle_pseudoinverse_information_relative_error,oracle_pseudoinverse_gradient_relative_error,"
            "oracle_information_covariance_dx_relative_error,oracle_information_covariance_covariance_relative_error\n";
}

void write_row(std::ostream &stream, const Fixture &fixture, const OracleResult &reference, const MethodRow &row) {
  stream << fixture.id << ',' << std::hex << kMasterSeed << std::dec << ',' << std::hex << fixture.seed << std::dec << ','
         << csv_escape(row.method) << ',' << csv_escape(row.implementation_label) << ',' << csv_escape(row.source_identity)
         << ',' << fixture.rows << ',' << fixture.layout.state_dimension << ',' << csv_escape(fixture.layout.name) << ','
         << csv_escape(fixture.geometry) << ',' << csv_escape(fixture.spectrum.name) << ',';
  write_double(stream, fixture.spectrum.target_condition);
  stream << ',' << fixture.spectrum.exact_rank << ',';
  write_double(stream, reference.condition_number);
  stream << ',' << reference.numerical_rank << ',' << static_cast<int>(fixture.calibration_active) << ",0,"
         << csv_escape(fixture.noise_model) << ',' << static_cast<int>(row.reduction.accepted) << ','
         << csv_escape(row.reduction.status) << ',' << csv_escape(row.reduction.reason) << ',' << static_cast<int>(row.finite)
         << ',';
  write_double(stream, row.reduced_information_relative_error);
  stream << ',';
  write_double(stream, row.reduced_gradient_relative_error);
  stream << ',';
  write_double(stream, row.state_increment_relative_error);
  stream << ',';
  write_double(stream, row.residual_cost_relative_error);
  stream << ',';
  write_double(stream, row.nis_relative_error);
  stream << ',';
  write_double(stream, row.posterior_covariance_relative_error);
  stream << ',';
  write_double(stream, row.posterior.covariance_symmetry_error_inf);
  stream << ',';
  write_double(stream, row.posterior.minimum_covariance_eigenvalue);
  stream << ',';
  write_double(stream, row.posterior.psd_tolerance);
  stream << ',' << static_cast<int>(row.reduction.input_mutation) << ',' << static_cast<int>(row.reduction.mutation_expected)
         << ',' << static_cast<int>(row.unexplained_input_mutation) << ',' << static_cast<int>(row.harmful_accepted) << ','
         << static_cast<int>(row.safe_rejection) << ',' << static_cast<int>(row.well_conditioned_parity) << ',';
  write_double(stream, row.reduction.runtime_ns);
  stream << ',';
  write_double(stream, reference.full_joint_dx_relative_error);
  stream << ',';
  write_double(stream, reference.full_joint_covariance_relative_error);
  stream << ',';
  write_double(stream, reference.pseudoinverse_information_relative_error);
  stream << ',';
  write_double(stream, reference.pseudoinverse_gradient_relative_error);
  stream << ',';
  write_double(stream, reference.information_covariance_dx_relative_error);
  stream << ',';
  write_double(stream, reference.information_covariance_covariance_relative_error);
  stream << '\n';
}

std::vector<SpectrumSpec> spectrum_specs() {
  std::vector<SpectrumSpec> spectra;
  const std::array<double, 7> conditions{{1.0, 1.0e2, 1.0e4, 1.0e6, 1.0e8, 1.0e10, 1.0e12}};
  for (const double condition : conditions) {
    SpectrumSpec spectrum;
    std::ostringstream name;
    name << "full_rank_cond_" << std::scientific << std::setprecision(0) << condition;
    spectrum.name = name.str();
    spectrum.target_condition = condition;
    spectrum.exact_rank = 3;
    spectrum.singular_values << 1.0, 1.0 / std::sqrt(condition), 1.0 / condition;
    spectra.push_back(spectrum);
  }
  spectra.push_back({"exact_rank_3", 4.0, 3, Eigen::Vector3d(1.0, 0.5, 0.25)});
  spectra.push_back({"exact_rank_2", std::numeric_limits<double>::infinity(), 2, Eigen::Vector3d(1.0, 0.1, 0.0)});
  spectra.push_back({"exact_rank_1", std::numeric_limits<double>::infinity(), 1, Eigen::Vector3d(1.0, 0.0, 0.0)});
  return spectra;
}

void write_metadata(const std::string &path, int fixture_count, int row_count) {
  std::ofstream metadata(path);
  if (!metadata) {
    throw std::runtime_error("cannot open metadata output " + path);
  }
  metadata << "schema=schurvio_conditioning_v1\n"
           << "master_seed=0x434f4e444954494f\n"
           << "fixture_count=" << fixture_count << "\n"
           << "method_row_count=" << row_count << "\n"
           << "timing_repetitions=" << kTimingRepetitions << "\n"
           << "precision=double\n"
           << "harmful_relative_threshold=1e-3\n"
           << "information_parity_relative_threshold=1e-8\n"
           << "posterior_parity_relative_threshold=1e-7\n"
           << "local_schur_source_sha256=" << kLocalSchurSourceSha256 << "\n"
           << "openvins_nullspace_source_sha256=" << kOpenVinsNullspaceSourceSha256 << "\n"
           << "openvins_nullspace_git_blob=" << kOpenVinsNullspaceGitBlob << "\n"
           << "timing_sink=" << std::setprecision(17) << g_timing_sink << "\n";
}

} // namespace

int main(int argc, char **argv) {
  if (argc != 3) {
    std::cerr << "usage: " << argv[0] << " OUTPUT.csv METADATA.txt\n";
    return 64;
  }
  try {
    std::ofstream output(argv[1]);
    if (!output) {
      throw std::runtime_error(std::string("cannot open CSV output ") + argv[1]);
    }
    write_header(output);

    const std::array<int, 6> row_counts{{4, 6, 8, 12, 20, 40}};
    const std::vector<SpectrumSpec> spectra = spectrum_specs();
    const std::array<LayoutSpec, 3> layouts{{
        {"openvins_4_clones_fixed_calibration", 39, false},
        {"openvins_11_clones_fixed_calibration", 81, false},
        {"openvins_11_clones_plus_camera_calibration", 110, true},
    }};
    const std::array<std::string, 7> general_geometries{{
        "healthy_parallax",
        "very_low_parallax",
        "far_depth",
        "near_collinear_rays",
        "repeated_view_geometry",
        "anisotropic_image_noise",
        "controlled_visual_outlier",
    }};
    const std::array<std::string, 2> calibration_geometries{{"calibration_inactive", "calibration_active"}};
    const std::array<std::string, 3> methods{{"L_SCHUR", "L_NS", "U_NS"}};

    int fixture_id = 0;
    int method_rows = 0;
    auto execute_fixture = [&](const Fixture &fixture) {
      const LinearSystem base_system{fixture.H_x, fixture.H_f, fixture.residual};
      const OracleResult base_oracle = compute_oracle(base_system, fixture.prior_covariance);
      if (!base_oracle.valid) {
        std::ostringstream message;
        message << "independent oracle failure at fixture " << fixture.id << " geometry=" << fixture.geometry
                << " spectrum=" << fixture.spectrum.name;
        throw std::runtime_error(message.str());
      }
      for (const std::string &method : methods) {
        const MethodRow row = evaluate_method(fixture, base_system, base_oracle, method);
        write_row(output, fixture, base_oracle, row);
        ++method_rows;
      }
    };

    for (const int rows : row_counts) {
      for (const SpectrumSpec &spectrum : spectra) {
        for (const std::string &geometry : general_geometries) {
          for (const LayoutSpec &layout : layouts) {
            execute_fixture(make_fixture(fixture_id++, rows, layout, spectrum, geometry));
          }
        }
        for (const std::string &geometry : calibration_geometries) {
          execute_fixture(make_fixture(fixture_id++, rows, layouts.back(), spectrum, geometry));
        }
      }
      output.flush();
      std::cerr << "completed observation_rows=" << rows << " fixtures=" << fixture_id << '\n';
    }
    if (fixture_id != 1380 || method_rows != 4140) {
      throw std::runtime_error("fixture population mismatch");
    }
    output.close();
    write_metadata(argv[2], fixture_id, method_rows);
    std::cout << "conditioning benchmark complete fixtures=" << fixture_id << " method_rows=" << method_rows << '\n';
    return 0;
  } catch (const std::exception &error) {
    std::cerr << "conditioning benchmark failed: " << error.what() << '\n';
    return 1;
  }
}
