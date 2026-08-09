/*
 * SchurVIO-Lite CP2 exact live-commit oracle tests.
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include "update/CP2CommitOracle.h"

#include <gtest/gtest.h>

#include <Eigen/Core>

#include <cmath>
#include <cstdint>
#include <limits>
#include <type_traits>
#include <utility>

namespace {

using ov_msckf::CP2ActiveStateType;
using ov_msckf::CP2ActiveStateTypeTag;
using ov_msckf::CP2CameraFixedCalibration;
using ov_msckf::CP2CameraModelCache;
using ov_msckf::CP2CommitOracle;
using ov_msckf::CP2CommitOracleResult;
using ov_msckf::CP2CommitOracleStatus;
using ov_msckf::CP2CompositeStateAdapter;
using ov_msckf::CP2CompositeStateSnapshot;
using ov_msckf::CP2CompositeStateStatus;
using ov_msckf::CP2FixedImuCalibration;
using ov_msckf::CP2FixedImuCalibrationTag;
using ov_msckf::CP2SemanticStateBlock;
using ov_msckf::CP2SemanticStateKind;
using ov_msckf::CP2StatePhase;

Eigen::MatrixXd vector_value(Eigen::Index rows, double first) {
  Eigen::MatrixXd value(rows, 1);
  for (Eigen::Index row = 0; row < rows; ++row) {
    value(row, 0) = first + static_cast<double>(row) / 16.0;
  }
  return value;
}

Eigen::MatrixXd unit_quaternion() {
  Eigen::MatrixXd value = Eigen::MatrixXd::Zero(4, 1);
  value(3, 0) = 1.0;
  return value;
}

CP2CompositeStateSnapshot make_phase0() {
  CP2CompositeStateSnapshot snapshot;
  snapshot.phase = CP2StatePhase::kPhase0Prior;
  snapshot.timestamp = 12.5;
  snapshot.covariance = Eigen::MatrixXd::Identity(15, 15);

  const CP2SemanticStateKind kinds[] = {
      CP2SemanticStateKind::kImuTheta,
      CP2SemanticStateKind::kImuPosition,
      CP2SemanticStateKind::kImuVelocity,
      CP2SemanticStateKind::kImuGyroBias,
      CP2SemanticStateKind::kImuAccelBias};
  for (std::uint64_t index = 0; index < UINT64_C(5); ++index) {
    CP2SemanticStateBlock block;
    block.kind = kinds[index];
    block.covariance_id = UINT64_C(3) * index;
    block.error_size = UINT64_C(3);
    snapshot.semantic_blocks.push_back(block);
  }

  CP2ActiveStateType imu;
  imu.tag = CP2ActiveStateTypeTag::kImu;
  imu.covariance_id = 0;
  imu.error_size = 15;
  imu.nominal = vector_value(16, 0.25);
  imu.fej = vector_value(16, 0.5);
  imu.nominal.block(0, 0, 4, 1) = unit_quaternion();
  imu.fej.block(0, 0, 4, 1) = unit_quaternion();
  snapshot.active_types.push_back(imu);

  const CP2FixedImuCalibrationTag fixed_tags[] = {
      CP2FixedImuCalibrationTag::kDw, CP2FixedImuCalibrationTag::kDa,
      CP2FixedImuCalibrationTag::kTg,
      CP2FixedImuCalibrationTag::kGyroToImu,
      CP2FixedImuCalibrationTag::kAccelToImu};
  const Eigen::Index fixed_rows[] = {6, 6, 9, 4, 4};
  for (std::size_t index = 0; index < 5U; ++index) {
    CP2FixedImuCalibration fixed;
    fixed.tag = fixed_tags[index];
    if (index < 3U) {
      const double scalar_index = static_cast<double>(index);
      fixed.nominal = vector_value(fixed_rows[index], 1.0 + scalar_index);
      fixed.fej = vector_value(fixed_rows[index], 2.0 + scalar_index);
    } else {
      fixed.nominal = unit_quaternion();
      fixed.fej = unit_quaternion();
    }
    snapshot.fixed_imu_calibrations.push_back(fixed);
  }

  snapshot.time_offset_nominal = Eigen::MatrixXd::Zero(1, 1);
  snapshot.time_offset_fej = Eigen::MatrixXd::Zero(1, 1);

  CP2CameraFixedCalibration camera;
  camera.camera_id = 0;
  camera.extrinsic_nominal = vector_value(7, 0.75);
  camera.extrinsic_fej = vector_value(7, 0.875);
  camera.extrinsic_nominal.block(0, 0, 4, 1) = unit_quaternion();
  camera.extrinsic_fej.block(0, 0, 4, 1) = unit_quaternion();
  camera.intrinsic_nominal = vector_value(8, 300.0);
  camera.intrinsic_fej = vector_value(8, 300.0);
  snapshot.camera_calibrations.push_back(camera);

  CP2CameraModelCache cache;
  cache.camera_id = 0;
  cache.width = 640;
  cache.height = 480;
  cache.calibration = camera.intrinsic_nominal;
  cache.K = Eigen::MatrixXd::Identity(3, 3);
  cache.D = Eigen::MatrixXd::Zero(4, 1);
  snapshot.camera_caches.push_back(cache);

  return snapshot;
}

struct ThreePhases {
  CP2CompositeStateSnapshot phase0;
  CP2CompositeStateSnapshot phase2;
  CP2CompositeStateSnapshot phase3;
};

ThreePhases make_matching_phases() {
  ThreePhases phases;
  phases.phase0 = make_phase0();
  phases.phase2 = phases.phase0;
  phases.phase2.phase = CP2StatePhase::kPhase2ExpectedPostcommit;
  phases.phase2.active_types[0].nominal(7, 0) += 0.125;
  phases.phase2.covariance(0, 0) = 0.875;
  phases.phase3 = phases.phase2;
  phases.phase3.phase = CP2StatePhase::kPhase3LivePostcommit;
  return phases;
}

void expect_zeroed_coefficient_population(
    const CP2CommitOracleResult &result) {
  EXPECT_EQ(result.baseline_verified_nominal_fields, UINT64_C(0));
  EXPECT_EQ(result.baseline_nominal_mismatches, UINT64_C(0));
  EXPECT_EQ(result.baseline_covariance_mismatches, UINT64_C(0));
  EXPECT_EQ(result.baseline_fej_mismatches, UINT64_C(0));
}

void expect_structural_failure(const ThreePhases &phases,
                               std::uint64_t expected_seen,
                               std::uint64_t expected_covariance_seen) {
  const CP2CommitOracleResult result =
      CP2CommitOracle::Compare(phases.phase0, phases.phase2, phases.phase3, 1);
  EXPECT_EQ(result.status, CP2CommitOracleStatus::kStructuralMismatch);
  EXPECT_FALSE(result.comparison_available);
  EXPECT_FALSE(result.structure_equal);
  EXPECT_FALSE(result.passed);
  EXPECT_EQ(result.baseline_expected_type_update_calls, UINT64_C(1));
  expect_zeroed_coefficient_population(result);
  EXPECT_EQ(result.state_blocks_expected, UINT64_C(5));
  EXPECT_EQ(result.state_blocks_seen, expected_seen);
  EXPECT_EQ(result.covariance_blocks_expected, UINT64_C(25));
  EXPECT_EQ(result.covariance_blocks_seen, expected_covariance_seen);
}

static_assert(
    noexcept(CP2CommitOracle::Compare(
        std::declval<const CP2CompositeStateSnapshot &>(),
        std::declval<const CP2CompositeStateSnapshot &>(),
        std::declval<const CP2CompositeStateSnapshot &>(), UINT64_C(0))),
    "the CP2 commit oracle must be statically nonthrowing");

TEST(CP2CommitOracle, MatchingCompositeHasExactPopulationsAndPasses) {
  const ThreePhases phases = make_matching_phases();
  ASSERT_EQ(CP2CompositeStateAdapter::Validate(phases.phase0),
            CP2CompositeStateStatus::kAccepted);
  ASSERT_EQ(CP2CompositeStateAdapter::Validate(phases.phase2),
            CP2CompositeStateStatus::kAccepted);
  ASSERT_EQ(CP2CompositeStateAdapter::Validate(phases.phase3),
            CP2CompositeStateStatus::kAccepted);

  const CP2CommitOracleResult result =
      CP2CommitOracle::Compare(phases.phase0, phases.phase2, phases.phase3, 1);
  EXPECT_EQ(result.status, CP2CommitOracleStatus::kComplete);
  EXPECT_STREQ(ov_msckf::cp2_commit_oracle_status_name(result.status),
               "complete");
  EXPECT_TRUE(result.comparison_available);
  EXPECT_TRUE(result.structure_equal);
  EXPECT_TRUE(result.phase0_valid);
  EXPECT_TRUE(result.phase2_valid);
  EXPECT_TRUE(result.phase3_valid);
  EXPECT_TRUE(result.complete_finite);
  EXPECT_TRUE(result.phase0_phase2_immutable_equal);
  EXPECT_TRUE(result.phase2_phase3_canonical_equal);
  EXPECT_TRUE(result.complete_canonical_equal);
  EXPECT_TRUE(result.type_update_calls_match);
  EXPECT_TRUE(result.passed);
  EXPECT_EQ(result.observed_type_update_calls, UINT64_C(1));
  EXPECT_EQ(result.baseline_expected_type_update_calls, UINT64_C(1));
  EXPECT_EQ(result.baseline_verified_nominal_fields, UINT64_C(16));
  EXPECT_EQ(result.baseline_nominal_mismatches, UINT64_C(0));
  EXPECT_EQ(result.baseline_covariance_mismatches, UINT64_C(0));
  EXPECT_EQ(result.baseline_fej_mismatches, UINT64_C(0));
  EXPECT_EQ(result.state_blocks_expected, UINT64_C(5));
  EXPECT_EQ(result.state_blocks_seen, UINT64_C(5));
  EXPECT_EQ(result.covariance_blocks_expected, UINT64_C(25));
  EXPECT_EQ(result.covariance_blocks_seen, UINT64_C(25));
}

TEST(CP2CommitOracle, CountsEachCoefficientMismatchClassByExactBits) {
  {
    ThreePhases phases = make_matching_phases();
    phases.phase3.active_types[0].nominal(8, 0) += 0.25;
    const CP2CommitOracleResult result = CP2CommitOracle::Compare(
        phases.phase0, phases.phase2, phases.phase3, 1);
    ASSERT_EQ(result.status, CP2CommitOracleStatus::kComplete);
    EXPECT_EQ(result.baseline_verified_nominal_fields, UINT64_C(16));
    EXPECT_EQ(result.baseline_nominal_mismatches, UINT64_C(1));
    EXPECT_EQ(result.baseline_covariance_mismatches, UINT64_C(0));
    EXPECT_EQ(result.baseline_fej_mismatches, UINT64_C(0));
    EXPECT_FALSE(result.phase2_phase3_canonical_equal);
    EXPECT_FALSE(result.passed);
  }
  {
    ThreePhases phases = make_matching_phases();
    phases.phase3.covariance(2, 9) += 0.25;
    phases.phase3.covariance(9, 2) += 0.25;
    const CP2CommitOracleResult result = CP2CommitOracle::Compare(
        phases.phase0, phases.phase2, phases.phase3, 1);
    ASSERT_EQ(result.status, CP2CommitOracleStatus::kComplete);
    EXPECT_EQ(result.baseline_verified_nominal_fields, UINT64_C(16));
    EXPECT_EQ(result.baseline_nominal_mismatches, UINT64_C(0));
    EXPECT_EQ(result.baseline_covariance_mismatches, UINT64_C(2));
    EXPECT_EQ(result.baseline_fej_mismatches, UINT64_C(0));
    EXPECT_FALSE(result.passed);
  }
  {
    ThreePhases phases = make_matching_phases();
    phases.phase2.active_types[0].fej(10, 0) += 0.25;
    phases.phase3.active_types[0].fej(10, 0) += 0.25;
    const CP2CommitOracleResult result = CP2CommitOracle::Compare(
        phases.phase0, phases.phase2, phases.phase3, 1);
    ASSERT_EQ(result.status, CP2CommitOracleStatus::kComplete);
    EXPECT_EQ(result.baseline_verified_nominal_fields, UINT64_C(16));
    EXPECT_EQ(result.baseline_nominal_mismatches, UINT64_C(0));
    EXPECT_EQ(result.baseline_covariance_mismatches, UINT64_C(0));
    EXPECT_EQ(result.baseline_fej_mismatches, UINT64_C(1));
    EXPECT_FALSE(result.passed);
  }
}

TEST(CP2CommitOracle,
     InventoryIdentityAndShapeFailuresZeroOnlyCoefficientPopulations) {
  {
    ThreePhases phases = make_matching_phases();
    phases.phase3.semantic_blocks.pop_back();
    expect_structural_failure(phases, UINT64_C(4), UINT64_C(16));
  }
  {
    ThreePhases phases = make_matching_phases();
    phases.phase3.semantic_blocks[2].covariance_id = UINT64_C(7);
    expect_structural_failure(phases, UINT64_C(5), UINT64_C(25));
  }
  {
    ThreePhases phases = make_matching_phases();
    phases.phase3.active_types[0].nominal.conservativeResize(15, 1);
    expect_structural_failure(phases, UINT64_C(5), UINT64_C(25));
  }
}

TEST(CP2CommitOracle, EqualNonfiniteBitsStillFailTheCompleteOracle) {
  ThreePhases phases = make_matching_phases();
  const double infinity = std::numeric_limits<double>::infinity();
  phases.phase2.active_types[0].nominal(9, 0) = infinity;
  phases.phase3.active_types[0].nominal(9, 0) = infinity;

  const CP2CommitOracleResult result =
      CP2CommitOracle::Compare(phases.phase0, phases.phase2, phases.phase3, 1);
  ASSERT_EQ(result.status, CP2CommitOracleStatus::kComplete);
  EXPECT_TRUE(result.structure_equal);
  EXPECT_FALSE(result.complete_finite);
  EXPECT_FALSE(result.phase2_valid);
  EXPECT_FALSE(result.phase3_valid);
  EXPECT_TRUE(result.phase2_phase3_canonical_equal);
  EXPECT_TRUE(result.complete_canonical_equal);
  EXPECT_EQ(result.baseline_verified_nominal_fields, UINT64_C(16));
  EXPECT_EQ(result.baseline_nominal_mismatches, UINT64_C(0));
  EXPECT_FALSE(result.passed);
}

TEST(CP2CommitOracle, EverySnapshotMustBeFiniteIndependently) {
  const double infinity = std::numeric_limits<double>::infinity();
  {
    ThreePhases phases = make_matching_phases();
    phases.phase0.active_types[0].nominal(9, 0) = infinity;
    const CP2CommitOracleResult result = CP2CommitOracle::Compare(
        phases.phase0, phases.phase2, phases.phase3, 1);
    ASSERT_EQ(result.status, CP2CommitOracleStatus::kComplete);
    EXPECT_FALSE(result.phase0_valid);
    EXPECT_TRUE(result.phase2_valid);
    EXPECT_TRUE(result.phase3_valid);
    EXPECT_FALSE(result.complete_finite);
    EXPECT_FALSE(result.passed);
  }
  {
    ThreePhases phases = make_matching_phases();
    phases.phase2.active_types[0].nominal(9, 0) = infinity;
    const CP2CommitOracleResult result = CP2CommitOracle::Compare(
        phases.phase0, phases.phase2, phases.phase3, 1);
    ASSERT_EQ(result.status, CP2CommitOracleStatus::kComplete);
    EXPECT_TRUE(result.phase0_valid);
    EXPECT_FALSE(result.phase2_valid);
    EXPECT_TRUE(result.phase3_valid);
    EXPECT_FALSE(result.complete_finite);
    EXPECT_FALSE(result.passed);
  }
  {
    ThreePhases phases = make_matching_phases();
    phases.phase3.active_types[0].nominal(9, 0) = infinity;
    const CP2CommitOracleResult result = CP2CommitOracle::Compare(
        phases.phase0, phases.phase2, phases.phase3, 1);
    ASSERT_EQ(result.status, CP2CommitOracleStatus::kComplete);
    EXPECT_TRUE(result.phase0_valid);
    EXPECT_TRUE(result.phase2_valid);
    EXPECT_FALSE(result.phase3_valid);
    EXPECT_FALSE(result.complete_finite);
    EXPECT_FALSE(result.passed);
  }
}

TEST(CP2CommitOracle, NonCoefficientCanonicalMismatchRetainsPopulations) {
  {
    ThreePhases phases = make_matching_phases();
    phases.phase3.timestamp += 0.25;
    const CP2CommitOracleResult result = CP2CommitOracle::Compare(
        phases.phase0, phases.phase2, phases.phase3, 1);
    ASSERT_EQ(result.status, CP2CommitOracleStatus::kComplete);
    EXPECT_TRUE(result.structure_equal);
    EXPECT_FALSE(result.phase2_phase3_canonical_equal);
    EXPECT_FALSE(result.complete_canonical_equal);
    EXPECT_EQ(result.baseline_verified_nominal_fields, UINT64_C(16));
    EXPECT_EQ(result.baseline_nominal_mismatches, UINT64_C(0));
    EXPECT_EQ(result.baseline_covariance_mismatches, UINT64_C(0));
    EXPECT_EQ(result.baseline_fej_mismatches, UINT64_C(0));
    EXPECT_FALSE(result.passed);
  }
  {
    ThreePhases phases = make_matching_phases();
    phases.phase2.camera_caches[0].D(2, 0) = 0.125;
    phases.phase3.camera_caches[0].D(2, 0) = 0.125;
    const CP2CommitOracleResult result = CP2CommitOracle::Compare(
        phases.phase0, phases.phase2, phases.phase3, 1);
    ASSERT_EQ(result.status, CP2CommitOracleStatus::kComplete);
    EXPECT_FALSE(result.phase0_phase2_immutable_equal);
    EXPECT_TRUE(result.phase2_phase3_canonical_equal);
    EXPECT_FALSE(result.complete_canonical_equal);
    EXPECT_EQ(result.baseline_verified_nominal_fields, UINT64_C(16));
    EXPECT_EQ(result.baseline_nominal_mismatches, UINT64_C(0));
    EXPECT_FALSE(result.passed);
  }
}

TEST(CP2CommitOracle, SignedZeroIsOneNominalBitMismatch) {
  ThreePhases phases = make_matching_phases();
  phases.phase2.active_types[0].nominal(11, 0) = 0.0;
  phases.phase3.active_types[0].nominal(11, 0) = -0.0;
  ASSERT_EQ(phases.phase2.active_types[0].nominal(11, 0),
            phases.phase3.active_types[0].nominal(11, 0));

  const CP2CommitOracleResult result =
      CP2CommitOracle::Compare(phases.phase0, phases.phase2, phases.phase3, 1);
  ASSERT_EQ(result.status, CP2CommitOracleStatus::kComplete);
  EXPECT_EQ(result.baseline_nominal_mismatches, UINT64_C(1));
  EXPECT_FALSE(result.phase2_phase3_canonical_equal);
  EXPECT_FALSE(result.passed);
}

TEST(CP2CommitOracle, DetachedTypeUpdateCallMismatchIsUpdateLevelFailure) {
  const ThreePhases phases = make_matching_phases();
  const CP2CommitOracleResult result =
      CP2CommitOracle::Compare(phases.phase0, phases.phase2, phases.phase3, 2);
  ASSERT_EQ(result.status, CP2CommitOracleStatus::kComplete);
  EXPECT_EQ(result.baseline_expected_type_update_calls, UINT64_C(1));
  EXPECT_EQ(result.observed_type_update_calls, UINT64_C(2));
  EXPECT_FALSE(result.type_update_calls_match);
  EXPECT_EQ(result.baseline_verified_nominal_fields, UINT64_C(16));
  EXPECT_FALSE(result.passed);
}

TEST(CP2CommitOracle, InvalidPhaseRetainsExpectedAndRowCountsButNoPopulation) {
  ThreePhases phases = make_matching_phases();
  phases.phase2.phase = CP2StatePhase::kPhase1Precommit;
  const CP2CommitOracleResult result =
      CP2CommitOracle::Compare(phases.phase0, phases.phase2, phases.phase3, 1);
  EXPECT_EQ(result.status, CP2CommitOracleStatus::kInvalidPhase);
  EXPECT_EQ(result.baseline_expected_type_update_calls, UINT64_C(1));
  EXPECT_EQ(result.state_blocks_expected, UINT64_C(5));
  EXPECT_EQ(result.state_blocks_seen, UINT64_C(5));
  EXPECT_EQ(result.covariance_blocks_expected, UINT64_C(25));
  EXPECT_EQ(result.covariance_blocks_seen, UINT64_C(25));
  expect_zeroed_coefficient_population(result);
}

TEST(CP2CommitOracle, CheckedIntegerHelpersNeverWrapOrClobberOnFailure) {
  std::uint64_t output = UINT64_C(0x435032);
  EXPECT_FALSE(ov_msckf::cp2_checked_eigen_index_to_u64(
      static_cast<Eigen::Index>(-1), output));
  EXPECT_EQ(output, UINT64_C(0x435032));
  EXPECT_TRUE(ov_msckf::cp2_checked_eigen_index_to_u64(
      static_cast<Eigen::Index>(37), output));
  EXPECT_EQ(output, UINT64_C(37));

  output = UINT64_C(0x435032);
  EXPECT_FALSE(ov_msckf::cp2_checked_add_u64(
      std::numeric_limits<std::uint64_t>::max(), UINT64_C(1), output));
  EXPECT_EQ(output, UINT64_C(0x435032));
  EXPECT_TRUE(ov_msckf::cp2_checked_add_u64(
      std::numeric_limits<std::uint64_t>::max(), UINT64_C(0), output));
  EXPECT_EQ(output, std::numeric_limits<std::uint64_t>::max());

  output = UINT64_C(0x435032);
  EXPECT_FALSE(ov_msckf::cp2_checked_multiply_u64(
      std::numeric_limits<std::uint64_t>::max(), UINT64_C(2), output));
  EXPECT_EQ(output, UINT64_C(0x435032));
  EXPECT_TRUE(ov_msckf::cp2_checked_multiply_u64(
      UINT64_C(0), std::numeric_limits<std::uint64_t>::max(), output));
  EXPECT_EQ(output, UINT64_C(0));
}

} // namespace
