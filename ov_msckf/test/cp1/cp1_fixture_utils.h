/*
 * SchurVIO-Lite CP1 mathematical test utilities.
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#ifndef SCHURVIO_LITE_CP1_FIXTURE_UTILS_H
#define SCHURVIO_LITE_CP1_FIXTURE_UTILS_H

#include <Eigen/Dense>
#include <Eigen/Eigenvalues>
#include <Eigen/SVD>

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <limits>
#include <string>

namespace schurvio_cp1 {

constexpr std::uint64_t kMasterSeed = 20260728ULL;
constexpr double kLandmarkRelativeSingularFloor = 1.0e-6;

class DeterministicRng {
public:
  explicit DeterministicRng(std::uint64_t seed) : state_(seed) {}

  std::uint64_t next_u64() {
    state_ += 0x9e3779b97f4a7c15ULL;
    std::uint64_t value = state_;
    value = (value ^ (value >> 30U)) * 0xbf58476d1ce4e5b9ULL;
    value = (value ^ (value >> 27U)) * 0x94d049bb133111ebULL;
    return value ^ (value >> 31U);
  }

  double uniform() {
    return static_cast<double>(next_u64() >> 11U) / 9007199254740992.0;
  }

  double symmetric(double scale = 1.0) { return scale * (2.0 * uniform() - 1.0); }

  // A deterministic, bounded normal-like variate. This deliberately avoids
  // std::normal_distribution, whose exact sequence is library-dependent.
  double normalish(double scale = 1.0) {
    double value = 0.0;
    for (int i = 0; i < 12; ++i) {
      value += uniform();
    }
    return scale * (value - 6.0);
  }

  int integer(int lower_inclusive, int upper_inclusive) {
    const std::uint64_t width = static_cast<std::uint64_t>(upper_inclusive - lower_inclusive + 1);
    return lower_inclusive + static_cast<int>(next_u64() % width);
  }

  Eigen::MatrixXd matrix(int rows, int cols, double scale = 1.0) {
    Eigen::MatrixXd value(rows, cols);
    for (int row = 0; row < rows; ++row) {
      for (int col = 0; col < cols; ++col) {
        value(row, col) = normalish(scale);
      }
    }
    return value;
  }

  Eigen::VectorXd vector(int rows, double scale = 1.0) {
    Eigen::VectorXd value(rows);
    for (int row = 0; row < rows; ++row) {
      value(row) = normalish(scale);
    }
    return value;
  }

private:
  std::uint64_t state_;
};

inline Eigen::MatrixXd orthonormal_columns(DeterministicRng &rng, int rows, int cols) {
  Eigen::MatrixXd raw = rng.matrix(rows, cols);
  Eigen::HouseholderQR<Eigen::MatrixXd> qr(raw);
  return qr.householderQ() * Eigen::MatrixXd::Identity(rows, cols);
}

inline Eigen::MatrixXd well_conditioned_landmark_jacobian(DeterministicRng &rng, int rows) {
  const Eigen::MatrixXd left = orthonormal_columns(rng, rows, 3);
  const Eigen::MatrixXd right = orthonormal_columns(rng, 3, 3);
  Eigen::Vector3d singular_values;
  singular_values << 1.5 + 0.5 * rng.uniform(), 0.9 + 0.3 * rng.uniform(), 0.35 + 0.2 * rng.uniform();
  return left * singular_values.asDiagonal() * right.transpose();
}

enum class FactorStatus {
  kAccepted,
  kNonfinite,
  kInsufficientRows,
  kRankDeficient,
  kIllConditioned,
};

inline const char *factor_status_name(FactorStatus status) {
  switch (status) {
  case FactorStatus::kAccepted:
    return "accepted";
  case FactorStatus::kNonfinite:
    return "nonfinite";
  case FactorStatus::kInsufficientRows:
    return "insufficient_rows";
  case FactorStatus::kRankDeficient:
    return "rank_deficient";
  case FactorStatus::kIllConditioned:
    return "ill_conditioned";
  }
  return "unknown";
}

struct SchurReduction {
  FactorStatus status = FactorStatus::kNonfinite;
  bool singular_values_available = false;
  bool singular_ratio_available = false;
  Eigen::Vector3d singular_values = Eigen::Vector3d::Zero();
  double singular_ratio = 0.0;
  int degrees_of_freedom = 0;
  double raw_symmetry_error_inf = 0.0;
  Eigen::MatrixXd lambda;
  Eigen::VectorXd eta;
  double gamma = 0.0;
  Eigen::MatrixXd landmark_u;
  Eigen::Matrix3d landmark_v = Eigen::Matrix3d::Identity();
};

inline double matrix_inf_norm(const Eigen::MatrixXd &matrix) {
  if (matrix.size() == 0) {
    return 0.0;
  }
  return matrix.cwiseAbs().rowwise().sum().maxCoeff();
}

inline SchurReduction reduce_landmark(const Eigen::MatrixXd &state_jacobian, const Eigen::MatrixXd &landmark_jacobian,
                                      const Eigen::VectorXd &residual) {
  SchurReduction result;
  if (landmark_jacobian.cols() != 3 || state_jacobian.rows() != landmark_jacobian.rows() ||
      residual.rows() != landmark_jacobian.rows()) {
    result.status = FactorStatus::kNonfinite;
    return result;
  }
  if (landmark_jacobian.rows() <= 3) {
    result.status = FactorStatus::kInsufficientRows;
    return result;
  }
  if (!state_jacobian.allFinite() || !landmark_jacobian.allFinite() || !residual.allFinite()) {
    result.status = FactorStatus::kNonfinite;
    return result;
  }

  Eigen::JacobiSVD<Eigen::MatrixXd> svd(landmark_jacobian, Eigen::ComputeThinU | Eigen::ComputeThinV);
  if (svd.singularValues().size() != 3 || !svd.singularValues().allFinite()) {
    result.status = FactorStatus::kNonfinite;
    return result;
  }
  result.singular_values = svd.singularValues();
  result.singular_values_available = true;
  const double largest = result.singular_values(0);
  const double smallest = result.singular_values(2);
  if (!(largest > std::numeric_limits<double>::min())) {
    result.status = FactorStatus::kRankDeficient;
    return result;
  }
  result.singular_ratio = smallest / largest;
  result.singular_ratio_available = true;
  const double numerical_floor =
      static_cast<double>(std::max(landmark_jacobian.rows(), landmark_jacobian.cols())) * std::numeric_limits<double>::epsilon() * largest;
  if (!(smallest > numerical_floor)) {
    result.status = FactorStatus::kRankDeficient;
    return result;
  }
  if (result.singular_ratio < kLandmarkRelativeSingularFloor) {
    result.status = FactorStatus::kIllConditioned;
    return result;
  }

  result.landmark_u = svd.matrixU();
  result.landmark_v = svd.matrixV();
  const Eigen::MatrixXd projected_state = result.landmark_u.transpose() * state_jacobian;
  const Eigen::Vector3d projected_residual = result.landmark_u.transpose() * residual;
  const Eigen::MatrixXd raw_lambda =
      state_jacobian.transpose() * state_jacobian - projected_state.transpose() * projected_state;
  result.raw_symmetry_error_inf = matrix_inf_norm(raw_lambda - raw_lambda.transpose());
  result.lambda = 0.5 * (raw_lambda + raw_lambda.transpose());
  result.eta = state_jacobian.transpose() * residual - projected_state.transpose() * projected_residual;
  result.gamma = residual.squaredNorm() - projected_residual.squaredNorm();
  result.degrees_of_freedom = landmark_jacobian.rows() - 3;
  result.status = FactorStatus::kAccepted;
  return result;
}

inline Eigen::Vector3d back_substitute_landmark(const SchurReduction &reduction, const Eigen::MatrixXd &state_jacobian,
                                                const Eigen::VectorXd &residual, const Eigen::VectorXd &state_increment) {
  Eigen::Vector3d coefficient = reduction.landmark_u.transpose() * (residual - state_jacobian * state_increment);
  for (int index = 0; index < 3; ++index) {
    coefficient(index) /= reduction.singular_values(index);
  }
  return reduction.landmark_v * coefficient;
}

inline double mixed_tolerance(double absolute, double relative, double reference_norm) {
  return absolute + relative * reference_norm;
}

inline Eigen::Matrix3d skew(const Eigen::Vector3d &value) {
  Eigen::Matrix3d result;
  result << 0.0, -value(2), value(1), value(2), 0.0, -value(0), -value(1), value(0), 0.0;
  return result;
}

inline Eigen::Matrix3d fixed_chart_orientation_map(const Eigen::Vector3d &absolute_increment) {
  return (Eigen::Matrix3d::Identity() - 0.5 * skew(absolute_increment)) /
         (1.0 + 0.25 * absolute_increment.squaredNorm());
}

} // namespace schurvio_cp1

#endif // SCHURVIO_LITE_CP1_FIXTURE_UTILS_H
