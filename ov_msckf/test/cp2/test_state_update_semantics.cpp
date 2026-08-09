/*
 * SchurVIO-Lite CP2-B clone, preview, and commit semantic tests.
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include "state/State.h"
#include "state/StateHelper.h"
#include "types/Type.h"
#include "update/SchurUpdate.h"
#include "update/UpdaterHelper.h"
#include "update/UpdaterMSCKFPreview.h"

#include <gtest/gtest.h>

#include <Eigen/Dense>
#include <Eigen/Eigenvalues>
#include <Eigen/QR>

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <iostream>
#include <limits>
#include <memory>
#include <utility>
#include <vector>

namespace {

constexpr std::uint64_t kStateSemanticSeed = 0x4350324253544154ULL;
constexpr int kStateSemanticFixtureCount = 128;
constexpr double kAbsoluteTolerance = 1.0e-8;
constexpr double kRelativeTolerance = 1.0e-6;

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

  double uniform() { return static_cast<double>(next_u64() >> 11U) / 9007199254740992.0; }

  double normalish(double scale = 1.0) {
    double value = 0.0;
    for (int index = 0; index < 12; ++index) {
      value += uniform();
    }
    return scale * (value - 6.0);
  }

  Eigen::MatrixXd matrix(int rows, int columns, double scale = 1.0) {
    Eigen::MatrixXd value(rows, columns);
    for (int row = 0; row < rows; ++row) {
      for (int column = 0; column < columns; ++column) {
        value(row, column) = normalish(scale);
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

double mixed_tolerance(double reference_norm) {
  return kAbsoluteTolerance + kRelativeTolerance * reference_norm;
}

double tolerance_ratio(double error, double reference_norm) {
  return error / mixed_tolerance(reference_norm);
}

std::uint64_t ieee_754_bits(double value) noexcept {
  static_assert(sizeof(double) == sizeof(std::uint64_t), "CP2 requires binary64 doubles");
  static_assert(std::numeric_limits<double>::is_iec559 && std::numeric_limits<double>::digits == 53 &&
                    std::numeric_limits<double>::max_exponent == 1024,
                "CP2 bitwise evidence requires IEEE-754 binary64 semantics");
  std::uint64_t bits = 0;
  std::memcpy(&bits, &value, sizeof(bits));
  return bits;
}

template <typename LeftDerived, typename RightDerived>
bool coefficient_bits_equal(const Eigen::MatrixBase<LeftDerived> &left,
                            const Eigen::MatrixBase<RightDerived> &right) {
  if (left.rows() != right.rows() || left.cols() != right.cols()) {
    return false;
  }
  for (Eigen::Index row = 0; row < left.rows(); ++row) {
    for (Eigen::Index column = 0; column < left.cols(); ++column) {
      if (ieee_754_bits(left(row, column)) != ieee_754_bits(right(row, column))) {
        return false;
      }
    }
  }
  return true;
}

struct SemanticBlockSpec {
  const char *name;
  bool is_clone;
  Eigen::Index nominal_offset;
  Eigen::Index nominal_size;
  Eigen::Index covariance_offset;
  Eigen::Index covariance_size;
};

const std::array<SemanticBlockSpec, 7> kSemanticBlocks{{
    {"theta", false, 0, 4, 0, 3},
    {"p", false, 4, 3, 3, 3},
    {"v", false, 7, 3, 6, 3},
    {"bg", false, 10, 3, 9, 3},
    {"ba", false, 13, 3, 12, 3},
    {"clone-theta", true, 0, 4, 0, 3},
    {"clone-p", true, 4, 3, 3, 3},
}};

void expect_no_repair_counters(const ov_msckf::MSCKFUpdatePreviewDiagnostics &diagnostics) {
  EXPECT_EQ(diagnostics.jitter_count, 0U);
  EXPECT_EQ(diagnostics.repair_count, 0U);
  EXPECT_EQ(diagnostics.alternate_solve_count, 0U);
  EXPECT_EQ(diagnostics.clamp_count, 0U);
  EXPECT_EQ(diagnostics.regularization_count, 0U);
  EXPECT_EQ(diagnostics.fallback_count, 0U);
}

Eigen::MatrixXd make_base_prior(DeterministicRng &rng) {
  constexpr int kImuErrorSize = 15;
  constexpr int kPoseErrorSize = 6;
  Eigen::MatrixXd root = Eigen::MatrixXd::Zero(kImuErrorSize, kImuErrorSize);
  for (int row = 0; row < kImuErrorSize; ++row) {
    for (int column = 0; column < row; ++column) {
      root(row, column) = rng.normalish(0.025);
    }
    root(row, row) = 0.22 + 0.08 * rng.uniform();
  }

  // Guarantee useful cross-covariance from the measured pose into the
  // directly unobserved velocity and bias block. Random cancellation can no
  // longer turn the corresponding semantic assertion into a vacuous check.
  for (int row = kPoseErrorSize; row < kImuErrorSize; ++row) {
    for (int column = 0; column < kPoseErrorSize; ++column) {
      const double sign = ((row + column) % 2 == 0) ? 1.0 : -1.0;
      root(row, column) += sign * (0.018 + 0.004 * rng.uniform());
    }
  }

  Eigen::MatrixXd prior = root * root.transpose();
  // Use one stored triangle as the exact covariance representation consumed
  // by StateHelper. The matrix still has the explicit L*L' PSD construction.
  prior = prior.selfadjointView<Eigen::Upper>();
  return prior;
}

Eigen::VectorXd make_imu_value(DeterministicRng &rng, bool fej) {
  Eigen::VectorXd value = Eigen::VectorXd::Zero(16);
  if (fej) {
    Eigen::Vector3d vector_part = rng.vector(3, 0.025);
    const double squared_norm = vector_part.squaredNorm();
    if (squared_norm >= 0.25) {
      vector_part *= 0.49 / std::sqrt(squared_norm);
    }
    value.head<3>() = vector_part;
    value(3) = std::sqrt(1.0 - vector_part.squaredNorm());
  } else {
    // Starting on the exact identity makes the error-state reset convention
    // observable without an unrelated quaternion chart conversion.
    value(3) = 1.0;
  }
  value.segment<12>(4) = rng.vector(12, fej ? 0.12 : 0.20);
  return value;
}

struct ClonedState {
  std::shared_ptr<ov_msckf::State> state;
  std::shared_ptr<ov_type::Type> clone;
};

ClonedState make_cloned_state(const Eigen::MatrixXd &base_prior, const Eigen::VectorXd &imu_value,
                              const Eigen::VectorXd &imu_fej) {
  ov_msckf::StateOptions options;
  options.do_fej = true;
  options.num_cameras = 0;
  auto state = std::make_shared<ov_msckf::State>(options);
  state->_imu->set_value(imu_value);
  state->_imu->set_fej(imu_fej);
  const std::vector<std::shared_ptr<ov_type::Type>> imu_order{state->_imu};
  ov_msckf::StateHelper::set_initial_covariance(state, base_prior, imu_order);

  // This exact production call is the subject of the CP2-B clone prior gate.
  std::shared_ptr<ov_type::Type> clone = ov_msckf::StateHelper::clone(state, state->_imu->pose());
  return {state, clone};
}

Eigen::MatrixXd expected_clone_copy_prior(const Eigen::MatrixXd &base_prior) {
  constexpr int kImuErrorSize = 15;
  constexpr int kPoseErrorSize = 6;
  Eigen::MatrixXd augmented = Eigen::MatrixXd::Zero(kImuErrorSize + kPoseErrorSize,
                                                    kImuErrorSize + kPoseErrorSize);
  augmented.topLeftCorner(kImuErrorSize, kImuErrorSize) = base_prior;
  augmented.topRightCorner(kImuErrorSize, kPoseErrorSize) = base_prior.leftCols(kPoseErrorSize);
  augmented.bottomLeftCorner(kPoseErrorSize, kImuErrorSize) = base_prior.topRows(kPoseErrorSize);
  augmented.bottomRightCorner(kPoseErrorSize, kPoseErrorSize) = base_prior.topLeftCorner(kPoseErrorSize, kPoseErrorSize);
  return augmented;
}

Eigen::MatrixXd orthonormal_columns(DeterministicRng &rng, int rows, int columns) {
  const Eigen::MatrixXd raw = rng.matrix(rows, columns);
  Eigen::HouseholderQR<Eigen::MatrixXd> qr(raw);
  return qr.householderQ() * Eigen::MatrixXd::Identity(rows, columns);
}

Eigen::MatrixXd make_landmark_jacobian(DeterministicRng &rng, int rows, int fixture) {
  const Eigen::MatrixXd left = orthonormal_columns(rng, rows, 3);
  const Eigen::Matrix3d right = orthonormal_columns(rng, 3, 3);
  Eigen::Vector3d singular_values;
  singular_values << 1.1 + 0.3 * rng.uniform(), 0.62 + 0.12 * rng.uniform(),
      0.24 + 0.06 * static_cast<double>(fixture % 5);
  return left * singular_values.asDiagonal() * right.transpose();
}

struct ReducedSystem {
  Eigen::MatrixXd H;
  Eigen::VectorXd residual;
  double noise_variance = std::numeric_limits<double>::quiet_NaN();
};

ReducedSystem reduce_baseline_and_compress(const Eigen::MatrixXd &H_x, const Eigen::MatrixXd &H_f,
                                           const Eigen::VectorXd &residual, double sigma_px) {
  Eigen::MatrixXd H_f_work = H_f;
  ReducedSystem result{H_x, residual, sigma_px * sigma_px};
  ov_msckf::UpdaterHelper::nullspace_project_inplace(H_f_work, result.H, result.residual);
  ov_msckf::UpdaterHelper::measurement_compress_inplace(result.H, result.residual);
  return result;
}

ReducedSystem reduce_schur_and_compress(const Eigen::MatrixXd &H_x, const Eigen::MatrixXd &H_f,
                                        const Eigen::VectorXd &residual, double sigma_px,
                                        ov_msckf::SchurReductionResult &reduction) {
  reduction = ov_msckf::SchurUpdate::Reduce(H_x, H_f, residual, sigma_px);
  ReducedSystem result{reduction.H_reduced, reduction.residual_reduced, reduction.noise_variance};
  if (reduction.accepted()) {
    ov_msckf::UpdaterHelper::measurement_compress_inplace(result.H, result.residual);
  }
  return result;
}

struct StateSnapshot {
  Eigen::MatrixXd covariance;
  Eigen::MatrixXd imu_value;
  Eigen::MatrixXd imu_fej;
  Eigen::MatrixXd clone_value;
  Eigen::MatrixXd clone_fej;
};

StateSnapshot snapshot(const ClonedState &bundle) {
  return {ov_msckf::StateHelper::get_full_covariance(bundle.state), bundle.state->_imu->value(),
          bundle.state->_imu->fej(), bundle.clone->value(), bundle.clone->fej()};
}

void expect_exact_snapshot(const ClonedState &bundle, const StateSnapshot &before) {
  EXPECT_TRUE(coefficient_bits_equal(ov_msckf::StateHelper::get_full_covariance(bundle.state), before.covariance));
  EXPECT_TRUE(coefficient_bits_equal(bundle.state->_imu->value(), before.imu_value));
  EXPECT_TRUE(coefficient_bits_equal(bundle.state->_imu->fej(), before.imu_fej));
  EXPECT_TRUE(coefficient_bits_equal(bundle.clone->value(), before.clone_value));
  EXPECT_TRUE(coefficient_bits_equal(bundle.clone->fej(), before.clone_fej));
}

Eigen::VectorXd semantic_nominal_block(const Eigen::MatrixXd &imu_value, const Eigen::MatrixXd &clone_value,
                                       const SemanticBlockSpec &spec) {
  const Eigen::MatrixXd &source = spec.is_clone ? clone_value : imu_value;
  return source.block(spec.nominal_offset, 0, spec.nominal_size, 1);
}

Eigen::Index semantic_covariance_id(const ClonedState &bundle, const SemanticBlockSpec &spec) {
  return (spec.is_clone ? bundle.clone->id() : bundle.state->_imu->id()) + spec.covariance_offset;
}

struct BlockCheckMetrics {
  double worst_ratio = 0.0;
  std::size_t checks = 0;
};

BlockCheckMetrics compare_semantic_nominal_blocks(const Eigen::MatrixXd &actual_imu,
                                                  const Eigen::MatrixXd &actual_clone,
                                                  const Eigen::MatrixXd &reference_imu,
                                                  const Eigen::MatrixXd &reference_clone,
                                                  const char *comparison) {
  BlockCheckMetrics metrics;
  for (const SemanticBlockSpec &spec : kSemanticBlocks) {
    const Eigen::VectorXd actual = semantic_nominal_block(actual_imu, actual_clone, spec);
    const Eigen::VectorXd reference = semantic_nominal_block(reference_imu, reference_clone, spec);
    const double ratio = tolerance_ratio((actual - reference).norm(), reference.norm());
    metrics.worst_ratio = std::max(metrics.worst_ratio, ratio);
    ++metrics.checks;
    EXPECT_LE(ratio, 1.0) << "comparison=" << comparison << " nominal_block=" << spec.name;
  }
  return metrics;
}

BlockCheckMetrics compare_semantic_covariance_blocks(const Eigen::MatrixXd &actual,
                                                     const ClonedState &actual_bundle,
                                                     const Eigen::MatrixXd &reference,
                                                     const ClonedState &reference_bundle,
                                                     const char *comparison) {
  BlockCheckMetrics metrics;
  for (const SemanticBlockSpec &row_spec : kSemanticBlocks) {
    for (const SemanticBlockSpec &column_spec : kSemanticBlocks) {
      const Eigen::Index actual_row = semantic_covariance_id(actual_bundle, row_spec);
      const Eigen::Index actual_column = semantic_covariance_id(actual_bundle, column_spec);
      const Eigen::Index reference_row = semantic_covariance_id(reference_bundle, row_spec);
      const Eigen::Index reference_column = semantic_covariance_id(reference_bundle, column_spec);
      const Eigen::MatrixXd actual_block =
          actual.block(actual_row, actual_column, row_spec.covariance_size, column_spec.covariance_size);
      const Eigen::MatrixXd reference_block =
          reference.block(reference_row, reference_column, row_spec.covariance_size, column_spec.covariance_size);
      const double ratio = tolerance_ratio((actual_block - reference_block).norm(), reference_block.norm());
      metrics.worst_ratio = std::max(metrics.worst_ratio, ratio);
      ++metrics.checks;
      EXPECT_LE(ratio, 1.0) << "comparison=" << comparison << " covariance_row=" << row_spec.name
                            << " covariance_column=" << column_spec.name;
    }
  }
  return metrics;
}

struct CommitMetrics {
  double preview_nominal_ratio = 0.0;
  double preview_covariance_ratio = 0.0;
  double identity_nominal_ratio = 0.0;
  double identity_covariance_ratio = 0.0;
  double minimum_unobserved_increment = std::numeric_limits<double>::infinity();
  std::size_t preview_nominal_checks = 0;
  std::size_t preview_covariance_checks = 0;
};

CommitMetrics commit_and_check_preview(const ClonedState &bundle, const ReducedSystem &system,
                                       const ov_msckf::MSCKFUpdatePreviewResult &preview) {
  CommitMetrics metrics;
  const StateSnapshot before = snapshot(bundle);
  const std::vector<std::shared_ptr<ov_type::Type>> H_order{bundle.clone};

  std::shared_ptr<ov_type::Type> expected_imu = bundle.state->_imu->clone();
  std::shared_ptr<ov_type::Type> expected_clone = bundle.clone->clone();
  expected_imu->update(preview.dx.segment(bundle.state->_imu->id(), bundle.state->_imu->size()));
  expected_clone->update(preview.dx.segment(bundle.clone->id(), bundle.clone->size()));

  const Eigen::MatrixXd R = system.noise_variance * Eigen::MatrixXd::Identity(system.H.rows(), system.H.rows());
  ov_msckf::StateHelper::EKFUpdate(bundle.state, H_order, system.H, system.residual, R);

  const Eigen::MatrixXd committed_covariance = ov_msckf::StateHelper::get_full_covariance(bundle.state);
  const BlockCheckMetrics nominal_metrics =
      compare_semantic_nominal_blocks(bundle.state->_imu->value(), bundle.clone->value(), expected_imu->value(),
                                      expected_clone->value(), "preview-vs-live");
  const BlockCheckMetrics covariance_metrics = compare_semantic_covariance_blocks(
      committed_covariance, bundle, preview.P_plus, bundle, "preview-vs-live");
  metrics.preview_nominal_ratio = nominal_metrics.worst_ratio;
  metrics.preview_covariance_ratio = covariance_metrics.worst_ratio;
  metrics.preview_nominal_checks = nominal_metrics.checks;
  metrics.preview_covariance_checks = covariance_metrics.checks;

  // FEJ values are immutable during both preview and commit.
  EXPECT_TRUE(coefficient_bits_equal(bundle.state->_imu->fej(), before.imu_fej));
  EXPECT_TRUE(coefficient_bits_equal(bundle.clone->fej(), before.clone_fej));

  // A cloned pose starts with the same nominal value and covariance row as
  // its source pose. Both receive the same error increment; the live updater
  // must retain that identity through its nonlinear Type::update operation
  // and through the covariance-form error-state reset (which is identity).
  const std::array<std::pair<std::size_t, std::size_t>, 2> pose_identity_pairs{{{0, 5}, {1, 6}}};
  for (const auto &identity_pair : pose_identity_pairs) {
    const SemanticBlockSpec &imu_spec = kSemanticBlocks.at(identity_pair.first);
    const SemanticBlockSpec &clone_spec = kSemanticBlocks.at(identity_pair.second);
    const Eigen::VectorXd reference = semantic_nominal_block(bundle.state->_imu->value(), bundle.clone->value(), imu_spec);
    const Eigen::VectorXd actual = semantic_nominal_block(bundle.state->_imu->value(), bundle.clone->value(), clone_spec);
    const double nominal_ratio = tolerance_ratio((actual - reference).norm(), reference.norm());
    metrics.identity_nominal_ratio = std::max(metrics.identity_nominal_ratio, nominal_ratio);
    EXPECT_LE(nominal_ratio, 1.0) << "identity_nominal_block=" << imu_spec.name;

    for (const SemanticBlockSpec &column_spec : kSemanticBlocks) {
      const Eigen::Index reference_row = semantic_covariance_id(bundle, imu_spec);
      const Eigen::Index actual_row = semantic_covariance_id(bundle, clone_spec);
      const Eigen::Index column = semantic_covariance_id(bundle, column_spec);
      const Eigen::MatrixXd reference_block = committed_covariance.block(
          reference_row, column, imu_spec.covariance_size, column_spec.covariance_size);
      const Eigen::MatrixXd actual_block = committed_covariance.block(
          actual_row, column, clone_spec.covariance_size, column_spec.covariance_size);
      const double covariance_ratio =
          tolerance_ratio((actual_block - reference_block).norm(), reference_block.norm());
      metrics.identity_covariance_ratio = std::max(metrics.identity_covariance_ratio, covariance_ratio);
      EXPECT_LE(covariance_ratio, 1.0) << "identity_covariance_row=" << imu_spec.name
                                      << " covariance_column=" << column_spec.name;
    }
  }

  // H_order contains only the clone. Velocity and both bias vectors are
  // therefore directly unobserved, so this nonzero change can arise only
  // through the full P*H' cross-covariance path.
  for (std::size_t index = 2; index <= 4; ++index) {
    const SemanticBlockSpec &spec = kSemanticBlocks.at(index);
    const Eigen::VectorXd after = semantic_nominal_block(bundle.state->_imu->value(), bundle.clone->value(), spec);
    const Eigen::VectorXd before_block = semantic_nominal_block(before.imu_value, before.clone_value, spec);
    const double increment = (after - before_block).norm();
    metrics.minimum_unobserved_increment = std::min(metrics.minimum_unobserved_increment, increment);
    EXPECT_GT(increment, 1.0e-12) << "directly_unobserved_block=" << spec.name;
  }
  return metrics;
}

struct StateComparisonMetrics {
  BlockCheckMetrics nominal;
  BlockCheckMetrics covariance;
};

StateComparisonMetrics compare_committed_states(const ClonedState &baseline, const ClonedState &candidate) {
  StateComparisonMetrics metrics;
  metrics.nominal = compare_semantic_nominal_blocks(
      candidate.state->_imu->value(), candidate.clone->value(), baseline.state->_imu->value(),
      baseline.clone->value(), "baseline-vs-schur");
  const Eigen::MatrixXd baseline_covariance = ov_msckf::StateHelper::get_full_covariance(baseline.state);
  const Eigen::MatrixXd candidate_covariance = ov_msckf::StateHelper::get_full_covariance(candidate.state);
  metrics.covariance = compare_semantic_covariance_blocks(candidate_covariance, candidate, baseline_covariance,
                                                          baseline, "baseline-vs-schur");
  return metrics;
}

TEST(CP2StateUpdateSemantics, ClonePreviewCompressionAndLiveCommitParity) {
  DeterministicRng rng(kStateSemanticSeed);
  const std::array<double, 8> sigma_values{{0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 4.0}};

  int clone_calls = 0;
  int global_compressions = 0;
  int accepted_previews = 0;
  int live_commits = 0;
  std::size_t preview_nominal_block_checks = 0;
  std::size_t preview_covariance_block_checks = 0;
  std::size_t mode_nominal_block_checks = 0;
  std::size_t mode_covariance_block_checks = 0;
  double worst_clone_prior_error = 0.0;
  double worst_clone_nullspace_ratio = 0.0;
  double worst_preview_nominal_ratio = 0.0;
  double worst_preview_covariance_ratio = 0.0;
  double worst_identity_nominal_ratio = 0.0;
  double worst_identity_covariance_ratio = 0.0;
  double worst_mode_nominal_ratio = 0.0;
  double worst_mode_covariance_block_ratio = 0.0;
  double minimum_unobserved_increment = std::numeric_limits<double>::infinity();

  for (int fixture = 0; fixture < kStateSemanticFixtureCount; ++fixture) {
    SCOPED_TRACE(::testing::Message() << "fixture=" << fixture);
    const Eigen::MatrixXd base_prior = make_base_prior(rng);
    const Eigen::VectorXd imu_value = make_imu_value(rng, false);
    const Eigen::VectorXd imu_fej = make_imu_value(rng, true);

    ClonedState baseline = make_cloned_state(base_prior, imu_value, imu_fej);
    ClonedState candidate = make_cloned_state(base_prior, imu_value, imu_fej);
    clone_calls += 2;
    ASSERT_TRUE(baseline.clone);
    ASSERT_TRUE(candidate.clone);
    ASSERT_EQ(baseline.clone->id(), 15);
    ASSERT_EQ(candidate.clone->id(), 15);

    const Eigen::MatrixXd expected_prior = expected_clone_copy_prior(base_prior);
    const Eigen::MatrixXd baseline_prior = ov_msckf::StateHelper::get_full_covariance(baseline.state);
    const Eigen::MatrixXd candidate_prior = ov_msckf::StateHelper::get_full_covariance(candidate.state);
    const double baseline_prior_error = (baseline_prior - expected_prior).norm();
    const double candidate_prior_error = (candidate_prior - expected_prior).norm();
    worst_clone_prior_error = std::max({worst_clone_prior_error, baseline_prior_error, candidate_prior_error});
    EXPECT_TRUE(coefficient_bits_equal(baseline_prior, expected_prior));
    EXPECT_TRUE(coefficient_bits_equal(candidate_prior, expected_prior));
    EXPECT_TRUE(coefficient_bits_equal(candidate_prior, baseline_prior));

    // The clone-copy prior has an exact six-dimensional structural
    // nullspace: [pose_error; clone_error] = [-d; d]. Check it directly,
    // without injecting noise or factoring the semidefinite covariance.
    Eigen::MatrixXd clone_nullspace = Eigen::MatrixXd::Zero(21, 6);
    clone_nullspace.topRows(6) = -Eigen::Matrix<double, 6, 6>::Identity();
    clone_nullspace.bottomRows(6) = Eigen::Matrix<double, 6, 6>::Identity();
    const double nullspace_ratio =
        (baseline_prior * clone_nullspace).norm() / (std::numeric_limits<double>::epsilon() * baseline_prior.norm());
    worst_clone_nullspace_ratio = std::max(worst_clone_nullspace_ratio, nullspace_ratio);
    EXPECT_LE(nullspace_ratio, 64.0);

    Eigen::SelfAdjointEigenSolver<Eigen::MatrixXd> prior_spectrum(baseline_prior);
    ASSERT_EQ(prior_spectrum.info(), Eigen::Success);
    const double prior_scale = std::max(1.0, prior_spectrum.eigenvalues().maxCoeff());
    EXPECT_GE(prior_spectrum.eigenvalues().minCoeff(),
              -128.0 * static_cast<double>(baseline_prior.rows()) * std::numeric_limits<double>::epsilon() * prior_scale);

    // More than six surviving rows guarantees that both production paths
    // execute the actual global compression routine instead of its early
    // return branch.
    const int raw_rows = 10 + fixture % 7;
    const Eigen::MatrixXd H_f = make_landmark_jacobian(rng, raw_rows, fixture);
    const Eigen::MatrixXd H_x = rng.matrix(raw_rows, 6, 0.30);
    Eigen::VectorXd residual = rng.vector(raw_rows, 0.08);
    residual(0) += 0.12;
    const double sigma_px = sigma_values[static_cast<std::size_t>(fixture) % sigma_values.size()];

    const ReducedSystem baseline_system = reduce_baseline_and_compress(H_x, H_f, residual, sigma_px);
    ov_msckf::SchurReductionResult schur_reduction;
    const ReducedSystem candidate_system =
        reduce_schur_and_compress(H_x, H_f, residual, sigma_px, schur_reduction);
    global_compressions += 2;
    ASSERT_TRUE(schur_reduction.accepted()) << ov_msckf::schur_reduction_stage_name(schur_reduction.stage);
    EXPECT_EQ(schur_reduction.jitter_count, 0U);
    EXPECT_EQ(schur_reduction.clamp_count, 0U);
    EXPECT_EQ(schur_reduction.regularization_count, 0U);
    EXPECT_EQ(schur_reduction.fallback_count, 0U);
    ASSERT_EQ(baseline_system.H.rows(), 6);
    ASSERT_EQ(candidate_system.H.rows(), 6);
    ASSERT_EQ(baseline_system.residual.rows(), 6);
    ASSERT_EQ(candidate_system.residual.rows(), 6);

    const std::vector<std::shared_ptr<ov_type::Type>> baseline_order{baseline.clone};
    const std::vector<std::shared_ptr<ov_type::Type>> candidate_order{candidate.clone};
    const Eigen::MatrixXd baseline_R =
        baseline_system.noise_variance * Eigen::MatrixXd::Identity(baseline_system.H.rows(), baseline_system.H.rows());
    const Eigen::MatrixXd candidate_R =
        candidate_system.noise_variance * Eigen::MatrixXd::Identity(candidate_system.H.rows(), candidate_system.H.rows());

    const StateSnapshot baseline_before_preview = snapshot(baseline);
    const StateSnapshot candidate_before_preview = snapshot(candidate);
    const ov_msckf::MSCKFUpdatePreviewResult baseline_preview = ov_msckf::UpdaterMSCKFPreview::Compute(
        baseline.state, baseline_order, baseline_system.H, baseline_system.residual, baseline_R);
    const ov_msckf::MSCKFUpdatePreviewResult candidate_preview = ov_msckf::UpdaterMSCKFPreview::Compute(
        candidate.state, candidate_order, candidate_system.H, candidate_system.residual, candidate_R);
    accepted_previews += 2;
    ASSERT_TRUE(baseline_preview.accepted())
        << ov_msckf::msckf_update_preview_stage_name(baseline_preview.diagnostics.stage);
    ASSERT_TRUE(candidate_preview.accepted())
        << ov_msckf::msckf_update_preview_stage_name(candidate_preview.diagnostics.stage);
    expect_no_repair_counters(baseline_preview.diagnostics);
    expect_no_repair_counters(candidate_preview.diagnostics);
    ASSERT_EQ(baseline_preview.dx.rows(), 21);
    ASSERT_EQ(candidate_preview.dx.rows(), 21);
    ASSERT_EQ(baseline_preview.P_plus.rows(), 21);
    ASSERT_EQ(candidate_preview.P_plus.rows(), 21);

    // Accepted preflight is as read-only as every rejected preflight below.
    expect_exact_snapshot(baseline, baseline_before_preview);
    expect_exact_snapshot(candidate, candidate_before_preview);

    const CommitMetrics baseline_metrics = commit_and_check_preview(baseline, baseline_system, baseline_preview);
    const CommitMetrics candidate_metrics = commit_and_check_preview(candidate, candidate_system, candidate_preview);
    live_commits += 2;
    worst_preview_nominal_ratio =
        std::max({worst_preview_nominal_ratio, baseline_metrics.preview_nominal_ratio,
                  candidate_metrics.preview_nominal_ratio});
    worst_preview_covariance_ratio =
        std::max({worst_preview_covariance_ratio, baseline_metrics.preview_covariance_ratio,
                  candidate_metrics.preview_covariance_ratio});
    preview_nominal_block_checks +=
        baseline_metrics.preview_nominal_checks + candidate_metrics.preview_nominal_checks;
    preview_covariance_block_checks +=
        baseline_metrics.preview_covariance_checks + candidate_metrics.preview_covariance_checks;
    worst_identity_nominal_ratio =
        std::max({worst_identity_nominal_ratio, baseline_metrics.identity_nominal_ratio,
                  candidate_metrics.identity_nominal_ratio});
    worst_identity_covariance_ratio =
        std::max({worst_identity_covariance_ratio, baseline_metrics.identity_covariance_ratio,
                  candidate_metrics.identity_covariance_ratio});
    minimum_unobserved_increment =
        std::min({minimum_unobserved_increment, baseline_metrics.minimum_unobserved_increment,
                  candidate_metrics.minimum_unobserved_increment});

    const StateComparisonMetrics mode_metrics = compare_committed_states(baseline, candidate);
    worst_mode_nominal_ratio = std::max(worst_mode_nominal_ratio, mode_metrics.nominal.worst_ratio);
    worst_mode_covariance_block_ratio =
        std::max(worst_mode_covariance_block_ratio, mode_metrics.covariance.worst_ratio);
    mode_nominal_block_checks += mode_metrics.nominal.checks;
    mode_covariance_block_checks += mode_metrics.covariance.checks;
  }

  EXPECT_EQ(clone_calls, 2 * kStateSemanticFixtureCount);
  EXPECT_EQ(global_compressions, 2 * kStateSemanticFixtureCount);
  EXPECT_EQ(accepted_previews, 2 * kStateSemanticFixtureCount);
  EXPECT_EQ(live_commits, 2 * kStateSemanticFixtureCount);
  EXPECT_EQ(preview_nominal_block_checks,
            2U * static_cast<std::size_t>(kStateSemanticFixtureCount) * kSemanticBlocks.size());
  EXPECT_EQ(preview_covariance_block_checks,
            2U * static_cast<std::size_t>(kStateSemanticFixtureCount) * kSemanticBlocks.size() *
                kSemanticBlocks.size());
  EXPECT_EQ(mode_nominal_block_checks,
            static_cast<std::size_t>(kStateSemanticFixtureCount) * kSemanticBlocks.size());
  EXPECT_EQ(mode_covariance_block_checks,
            static_cast<std::size_t>(kStateSemanticFixtureCount) * kSemanticBlocks.size() *
                kSemanticBlocks.size());
  EXPECT_GT(minimum_unobserved_increment, 1.0e-12);
  std::cout << "CP2_B_STATE_UPDATE fixtures=" << kStateSemanticFixtureCount << " seed=" << kStateSemanticSeed
            << " clone_calls=" << clone_calls << " global_compressions=" << global_compressions
            << " accepted_previews=" << accepted_previews << " live_commits=" << live_commits
            << " max_clone_prior_error=" << worst_clone_prior_error
            << " max_clone_nullspace_ratio=" << worst_clone_nullspace_ratio
            << " max_preview_nominal_tolerance_ratio=" << worst_preview_nominal_ratio
            << " max_preview_covariance_tolerance_ratio=" << worst_preview_covariance_ratio
            << " max_identity_nominal_tolerance_ratio=" << worst_identity_nominal_ratio
            << " max_identity_covariance_tolerance_ratio=" << worst_identity_covariance_ratio
            << " max_mode_nominal_tolerance_ratio=" << worst_mode_nominal_ratio
            << " max_mode_covariance_block_tolerance_ratio=" << worst_mode_covariance_block_ratio
            << " preview_nominal_block_checks=" << preview_nominal_block_checks
            << " preview_covariance_block_checks=" << preview_covariance_block_checks
            << " mode_nominal_block_checks=" << mode_nominal_block_checks
            << " mode_covariance_block_checks=" << mode_covariance_block_checks
            << " min_unobserved_velocity_bias_increment=" << minimum_unobserved_increment << std::endl;
}

TEST(CP2StateUpdateSemantics, InvalidPreviewInputsAreBitwiseReadOnlyAndNeverRepaired) {
  DeterministicRng rng(kStateSemanticSeed ^ 0x494e56414c494455ULL);
  const Eigen::MatrixXd base_prior = make_base_prior(rng);
  ClonedState bundle = make_cloned_state(base_prior, make_imu_value(rng, false), make_imu_value(rng, true));
  ASSERT_TRUE(bundle.clone);
  const StateSnapshot before = snapshot(bundle);

  const std::vector<std::shared_ptr<ov_type::Type>> order{bundle.clone};
  const Eigen::MatrixXd H = rng.matrix(4, 6, 0.2);
  const Eigen::VectorXd residual = rng.vector(4, 0.05);
  const Eigen::MatrixXd R = Eigen::MatrixXd::Identity(4, 4);
  int rejected_cases = 0;

  const auto check_rejected = [&](const std::vector<std::shared_ptr<ov_type::Type>> &test_order,
                                  const Eigen::MatrixXd &test_H, const Eigen::VectorXd &test_residual,
                                  const Eigen::MatrixXd &test_R, ov_msckf::MSCKFUpdatePreviewStatus expected_status,
                                  ov_msckf::MSCKFUpdatePreviewStage expected_stage,
                                  bool expect_negative_posterior_diagonal) {
    const ov_msckf::MSCKFUpdatePreviewResult result =
        ov_msckf::UpdaterMSCKFPreview::Compute(bundle.state, test_order, test_H, test_residual, test_R);
    EXPECT_FALSE(result.accepted());
    EXPECT_EQ(result.diagnostics.status, expected_status);
    EXPECT_EQ(result.diagnostics.stage, expected_stage);
    EXPECT_EQ(result.dx.size(), 0);
    EXPECT_EQ(result.P_plus.size(), 0);
    expect_no_repair_counters(result.diagnostics);
    if (expect_negative_posterior_diagonal) {
      EXPECT_TRUE(result.diagnostics.minimum_posterior_diagonal_available);
      EXPECT_LT(result.diagnostics.minimum_posterior_diagonal, 0.0);
      EXPECT_GE(result.diagnostics.offending_diagonal_index, 0);
    }
    expect_exact_snapshot(bundle, before);
    ++rejected_cases;
  };

  check_rejected(order, H.topRows(3), residual, R, ov_msckf::MSCKFUpdatePreviewStatus::kInvalidInput,
                 ov_msckf::MSCKFUpdatePreviewStage::kInputDimensions, false);

  Eigen::MatrixXd nonfinite_H = H;
  nonfinite_H(1, 2) = std::numeric_limits<double>::quiet_NaN();
  check_rejected(order, nonfinite_H, residual, R, ov_msckf::MSCKFUpdatePreviewStatus::kNonfinite,
                 ov_msckf::MSCKFUpdatePreviewStage::kRawInputs, false);

  const std::vector<std::shared_ptr<ov_type::Type>> duplicate_order{bundle.clone, bundle.clone};
  check_rejected(duplicate_order, rng.matrix(4, 12, 0.2), residual, R,
                 ov_msckf::MSCKFUpdatePreviewStatus::kInvalidInput,
                 ov_msckf::MSCKFUpdatePreviewStage::kStateOrder, false);

  const Eigen::MatrixXd indefinite_R = -1000.0 * Eigen::MatrixXd::Identity(4, 4);
  check_rejected(order, H, residual, indefinite_R, ov_msckf::MSCKFUpdatePreviewStatus::kFactorizationFailed,
                 ov_msckf::MSCKFUpdatePreviewStage::kInnovationFactorization, false);

  // Deliberately use a negative scalar measurement variance whose innovation
  // remains positive. The LLT therefore succeeds, but the subtractive update
  // makes the directly measured clone variance negative. This reaches the
  // exact posterior-diagonal rejection stage instead of merely exercising an
  // earlier malformed-input or failed-factorization path.
  const Eigen::MatrixXd prior = ov_msckf::StateHelper::get_full_covariance(bundle.state);
  Eigen::MatrixXd negative_posterior_H = Eigen::MatrixXd::Zero(1, bundle.clone->size());
  negative_posterior_H(0, 0) = 1.0;
  const double measured_variance = prior(bundle.clone->id(), bundle.clone->id());
  ASSERT_TRUE(std::isfinite(measured_variance));
  ASSERT_GT(measured_variance, 0.0);
  const Eigen::VectorXd negative_posterior_residual = Eigen::VectorXd::Constant(1, 0.01);
  const Eigen::MatrixXd negative_posterior_R =
      Eigen::MatrixXd::Constant(1, 1, -0.5 * measured_variance);
  check_rejected(order, negative_posterior_H, negative_posterior_residual, negative_posterior_R,
                 ov_msckf::MSCKFUpdatePreviewStatus::kNegativeDiagonal,
                 ov_msckf::MSCKFUpdatePreviewStage::kPosteriorDiagonal, true);

  EXPECT_EQ(rejected_cases, 5);
  std::cout << "CP2_B_PREVIEW_REJECTION cases=" << rejected_cases << " seed="
            << (kStateSemanticSeed ^ 0x494e56414c494455ULL)
            << " state_mutations=0 jitter_count=0 repair_count=0 alternate_solve_count=0 clamp_count=0"
               " regularization_count=0 fallback_count=0"
            << std::endl;
}

} // namespace
