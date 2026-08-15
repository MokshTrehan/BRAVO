/*
 * TurnSafe T1 certificate-core tests.
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include <gtest/gtest.h>

#include "update/TurnSafeCertificate.h"

#include <Eigen/Geometry>

#include <boost/math/distributions/chi_squared.hpp>
#include <boost/math/distributions/normal.hpp>

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <limits>
#include <random>
#include <string>

namespace {

using ov_msckf::TurnSafeBearingResult;
using ov_msckf::TurnSafeCapturedPairCovariance;
using ov_msckf::TurnSafeCertificate;
using ov_msckf::TurnSafeCertificateCamera;
using ov_msckf::TurnSafeCertificateClonePose;
using ov_msckf::TurnSafeCertificateObservation;
using ov_msckf::TurnSafeCertificateStatus;

TurnSafeCertificateCamera camera(std::size_t id,
                                 const Eigen::Vector3d &center_in_imu) {
  TurnSafeCertificateCamera output;
  output.camera_id = id;
  output.intrinsics << 400.0, 405.0, 320.0, 240.0, -0.05, 0.01,
      0.001, -0.0005;
  output.R_ItoC.setIdentity();
  output.p_IinC = -center_in_imu;
  output.width = 640;
  output.height = 480;
  return output;
}

Eigen::Vector2d normalized_for_point(
    const TurnSafeCertificateCamera &calibration,
    const Eigen::Vector3d &point_in_imu) {
  const Eigen::Vector3d point_in_camera =
      calibration.R_ItoC * point_in_imu + calibration.p_IinC;
  return Eigen::Vector2d(point_in_camera(0) / point_in_camera(2),
                         point_in_camera(1) / point_in_camera(2));
}

Eigen::Vector2d raw_for_normalized(
    const TurnSafeCertificateCamera &calibration,
    const Eigen::Vector2d &normalized) {
  return TurnSafeCertificate::CamRadtanForward(calibration.intrinsics,
                                                normalized)
      .raw_pixel;
}

TurnSafeCertificateObservation observation_for_point(
    const TurnSafeCertificateCamera &calibration,
    const Eigen::Vector3d &point_in_imu, std::uint64_t feature_id = 7U,
    std::uint64_t detached_index = 3U, double timestamp = 12.5) {
  TurnSafeCertificateObservation output;
  output.feature_id = feature_id;
  output.detached_index = detached_index;
  output.camera_id = calibration.camera_id;
  output.timestamp = timestamp;
  output.captured_normalized =
      normalized_for_point(calibration, point_in_imu);
  output.raw_pixel =
      raw_for_normalized(calibration, output.captured_normalized);
  return output;
}

TurnSafeCertificateObservation observation_for_raw(
    const TurnSafeCertificateCamera &calibration,
    const Eigen::Vector2d &raw_pixel,
    const TurnSafeCertificateObservation &identity) {
  TurnSafeCertificateObservation output = identity;
  output.raw_pixel = raw_pixel;
  const auto inverse =
      TurnSafeCertificate::CamRadtanInverse(calibration, raw_pixel);
  output.captured_normalized = inverse.normalized;
  return output;
}

Eigen::Vector3d bearing_for_raw(const TurnSafeCertificateCamera &calibration,
                                const Eigen::Vector2d &raw_pixel) {
  const auto inverse =
      TurnSafeCertificate::CamRadtanInverse(calibration, raw_pixel);
  Eigen::Vector3d homogeneous(inverse.normalized(0), inverse.normalized(1),
                              1.0);
  return homogeneous.normalized();
}

Eigen::Matrix<double, 3, 2> tangent_basis(
    const Eigen::Vector3d &bearing) {
  Eigen::Index axis_index = 0;
  bearing.cwiseAbs().minCoeff(&axis_index);
  Eigen::Vector3d axis = Eigen::Vector3d::Zero();
  axis(axis_index) = 1.0;
  const Eigen::Vector3d first =
      (axis - bearing * bearing.dot(axis)).normalized();
  Eigen::Matrix<double, 3, 2> output;
  output.col(0) = first;
  output.col(1) = bearing.cross(first);
  return output;
}

Eigen::Matrix3d rotation(const Eigen::Vector3d &axis, double angle) {
  return Eigen::AngleAxisd(angle, axis.normalized()).toRotationMatrix();
}

Eigen::Vector3d relative_camera_center_mean(
    const TurnSafeCertificateCamera &calibration,
    const TurnSafeCertificateClonePose &source,
    const TurnSafeCertificateClonePose &target) {
  const Eigen::Vector3d lever =
      calibration.R_ItoC.transpose() * calibration.p_IinC;
  const Eigen::Vector3d source_center =
      source.p_IinG - source.R_GtoI.transpose() * lever;
  const Eigen::Vector3d target_center =
      target.p_IinG - target.R_GtoI.transpose() * lever;
  return source_center - target_center;
}

TurnSafeCapturedPairCovariance dense_pair_covariance() {
  Eigen::Matrix<double, 12, 12> lower =
      0.02 * Eigen::Matrix<double, 12, 12>::Identity();
  for (Eigen::Index row = 1; row < lower.rows(); ++row) {
    for (Eigen::Index col = 0; col < row; ++col) {
      lower(row, col) =
          2.0e-4 * static_cast<double>(((row + 2 * col) % 7) - 3);
    }
  }
  const Eigen::Matrix<double, 12, 12> covariance =
      lower * lower.transpose();
  TurnSafeCapturedPairCovariance output;
  output.P_ss = covariance.block<6, 6>(0, 0);
  output.P_st = covariance.block<6, 6>(0, 6);
  output.P_ts = covariance.block<6, 6>(6, 0);
  output.P_tt = covariance.block<6, 6>(6, 6);
  return output;
}

TEST(TurnSafeCertificateCamRadtan,
     ExactForwardJacobianMatchesCentralFiniteDifferences) {
  const TurnSafeCertificateCamera calibration =
      camera(0U, Eigen::Vector3d::Zero());
  constexpr double epsilon = 1.0e-7;
  for (const Eigen::Vector2d &normalized :
       {Eigen::Vector2d(0.0, 0.0), Eigen::Vector2d(0.31, -0.22),
        Eigen::Vector2d(-0.47, 0.36)}) {
    const auto analytic = TurnSafeCertificate::CamRadtanForward(
        calibration.intrinsics, normalized);
    ASSERT_TRUE(analytic.available());
    Eigen::Matrix2d finite_difference;
    for (Eigen::Index column = 0; column < 2; ++column) {
      Eigen::Vector2d offset = Eigen::Vector2d::Zero();
      offset(column) = epsilon;
      const auto plus = TurnSafeCertificate::CamRadtanForward(
          calibration.intrinsics, normalized + offset);
      const auto minus = TurnSafeCertificate::CamRadtanForward(
          calibration.intrinsics, normalized - offset);
      ASSERT_TRUE(plus.available());
      ASSERT_TRUE(minus.available());
      finite_difference.col(column) =
          (plus.raw_pixel - minus.raw_pixel) / (2.0 * epsilon);
    }
    EXPECT_LE((analytic.pixel_wrt_normalized - finite_difference).norm(),
              2.0e-6);
  }
}

TEST(TurnSafeCertificateCamRadtan,
     CheckedNewtonAndCapturedNormalizedAuditFailClosed) {
  const TurnSafeCertificateCamera calibration =
      camera(0U, Eigen::Vector3d::Zero());
  const Eigen::Vector2d normalized(0.37, -0.29);
  const Eigen::Vector2d raw = raw_for_normalized(calibration, normalized);
  const auto inverse =
      TurnSafeCertificate::CamRadtanInverse(calibration, raw);
  ASSERT_TRUE(inverse.available());
  EXPECT_LE((inverse.normalized - normalized).norm(), 2.0e-15);
  EXPECT_LE(inverse.final_pixel_inf_norm,
            TurnSafeCertificate::kCamRadtanNewtonPixelInfNorm);
  EXPECT_GE(inverse.reciprocal_condition,
            TurnSafeCertificate::kChecked2x2ReciprocalConditionMinimum);

  const auto bearing = TurnSafeCertificate::BearingFromCapturedNormalized(
      calibration, raw, normalized);
  ASSERT_TRUE(bearing.available());
  EXPECT_NEAR(bearing.bearing.norm(), 1.0, 2.0e-15);

  const auto inconsistent =
      TurnSafeCertificate::BearingFromCapturedNormalized(
          calibration, raw, normalized + Eigen::Vector2d(1.0e-4, 0.0));
  EXPECT_EQ(inconsistent.status,
            TurnSafeCertificateStatus::kCapturedNormalizedInconsistent);

  const auto outside = TurnSafeCertificate::CamRadtanInverse(
      calibration, Eigen::Vector2d(-1.0, 20.0));
  EXPECT_EQ(outside.status,
            TurnSafeCertificateStatus::kPixelOutsideImage);
}

TEST(TurnSafeCertificateBearing,
     PixelToUnitBearingJacobianMatchesCentralFiniteDifferences) {
  const TurnSafeCertificateCamera calibration =
      camera(0U, Eigen::Vector3d::Zero());
  const Eigen::Vector2d normalized(0.22, -0.18);
  const Eigen::Vector2d raw = raw_for_normalized(calibration, normalized);
  const TurnSafeBearingResult analytic =
      TurnSafeCertificate::BearingFromCapturedNormalized(calibration, raw,
                                                         normalized);
  ASSERT_TRUE(analytic.available());
  constexpr double epsilon = 1.0e-4;
  Eigen::Matrix<double, 3, 2> finite_difference;
  for (Eigen::Index column = 0; column < 2; ++column) {
    Eigen::Vector2d offset = Eigen::Vector2d::Zero();
    offset(column) = epsilon;
    finite_difference.col(column) =
        (bearing_for_raw(calibration, raw + offset) -
         bearing_for_raw(calibration, raw - offset)) /
        (2.0 * epsilon);
  }
  EXPECT_LE((analytic.bearing_wrt_pixel - finite_difference).norm(),
            2.0e-10);
}

TEST(TurnSafeCertificateBearing,
     LinearizedBearingCovarianceMatchesDeterministicMonteCarlo) {
  const TurnSafeCertificateCamera calibration =
      camera(0U, Eigen::Vector3d::Zero());
  const Eigen::Vector2d normalized(0.18, -0.12);
  const Eigen::Vector2d raw = raw_for_normalized(calibration, normalized);
  const TurnSafeBearingResult bearing =
      TurnSafeCertificate::BearingFromCapturedNormalized(calibration, raw,
                                                         normalized);
  ASSERT_TRUE(bearing.available());
  const Eigen::Matrix3d predicted =
      TurnSafeCertificate::kPixelNoiseSigma *
      TurnSafeCertificate::kPixelNoiseSigma *
      bearing.bearing_wrt_pixel * bearing.bearing_wrt_pixel.transpose();

  std::mt19937_64 generator(UINT64_C(0x7475726e73616665));
  std::normal_distribution<double> noise(
      0.0, TurnSafeCertificate::kPixelNoiseSigma);
  Eigen::Matrix3d empirical = Eigen::Matrix3d::Zero();
  constexpr int samples = 12000;
  for (int sample = 0; sample < samples; ++sample) {
    const Eigen::Vector2d perturbed =
        raw + Eigen::Vector2d(noise(generator), noise(generator));
    const Eigen::Vector3d delta =
        bearing_for_raw(calibration, perturbed) - bearing.bearing;
    empirical.noalias() += delta * delta.transpose();
  }
  empirical /= static_cast<double>(samples);
  const double relative_error =
      (empirical - predicted).norm() / predicted.norm();
  RecordProperty("monte_carlo_samples", samples);
  RecordProperty("relative_covariance_error", std::to_string(relative_error));
  EXPECT_LE(relative_error, 0.04);
}

TEST(TurnSafeCertificateStereoRange,
     AnalyticPixelJacobianMatchesCentralFiniteDifferences) {
  const TurnSafeCertificateCamera main_camera =
      camera(0U, Eigen::Vector3d::Zero());
  const TurnSafeCertificateCamera mate_camera =
      camera(1U, Eigen::Vector3d(0.20, 0.0, 0.0));
  const Eigen::Vector3d point(0.35, -0.12, 4.0);
  const TurnSafeCertificateObservation main =
      observation_for_point(main_camera, point);
  const TurnSafeCertificateObservation mate =
      observation_for_point(mate_camera, point);
  const auto analytic = TurnSafeCertificate::TargetTimeStereoRange(
      main_camera, main, mate_camera, mate);
  ASSERT_TRUE(analytic.available())
      << ov_msckf::turnsafe_certificate_status_name(analytic.status);
  EXPECT_NEAR(analytic.d_hat, point.norm(), 2.0e-12);
  EXPECT_GT(analytic.d_lcb, 0.0);
  EXPECT_GE(analytic.ray_reciprocal_condition,
            TurnSafeCertificate::kStereoRayReciprocalConditionMinimum);

  constexpr double epsilon = 1.0e-3;
  for (Eigen::Index column = 0; column < 4; ++column) {
    TurnSafeCertificateObservation main_plus = main;
    TurnSafeCertificateObservation main_minus = main;
    TurnSafeCertificateObservation mate_plus = mate;
    TurnSafeCertificateObservation mate_minus = mate;
    if (column < 2) {
      Eigen::Vector2d plus_raw = main.raw_pixel;
      Eigen::Vector2d minus_raw = main.raw_pixel;
      plus_raw(column) += epsilon;
      minus_raw(column) -= epsilon;
      main_plus = observation_for_raw(main_camera, plus_raw, main);
      main_minus = observation_for_raw(main_camera, minus_raw, main);
    } else {
      Eigen::Vector2d plus_raw = mate.raw_pixel;
      Eigen::Vector2d minus_raw = mate.raw_pixel;
      plus_raw(column - 2) += epsilon;
      minus_raw(column - 2) -= epsilon;
      mate_plus = observation_for_raw(mate_camera, plus_raw, mate);
      mate_minus = observation_for_raw(mate_camera, minus_raw, mate);
    }
    const auto plus = TurnSafeCertificate::TargetTimeStereoRange(
        main_camera, main_plus, mate_camera, mate_plus);
    const auto minus = TurnSafeCertificate::TargetTimeStereoRange(
        main_camera, main_minus, mate_camera, mate_minus);
    ASSERT_TRUE(plus.available());
    ASSERT_TRUE(minus.available());
    const double finite_difference =
        (plus.d_hat - minus.d_hat) / (2.0 * epsilon);
    EXPECT_NEAR(analytic.range_wrt_pixels(column), finite_difference,
                2.0e-7 * std::max(1.0, std::fabs(finite_difference)));
  }
}

TEST(TurnSafeCertificateStereoRange,
     IdentityTimestampAndSingularGeometryAreTypedFailures) {
  const TurnSafeCertificateCamera main_camera =
      camera(0U, Eigen::Vector3d::Zero());
  const TurnSafeCertificateCamera mate_camera =
      camera(1U, Eigen::Vector3d(0.20, 0.0, 0.0));
  const Eigen::Vector3d point(0.2, 0.0, 4.0);
  const TurnSafeCertificateObservation main =
      observation_for_point(main_camera, point);
  TurnSafeCertificateObservation mate =
      observation_for_point(mate_camera, point);

  mate.feature_id += 1U;
  EXPECT_EQ(TurnSafeCertificate::TargetTimeStereoRange(
                main_camera, main, mate_camera, mate)
                .status,
            TurnSafeCertificateStatus::kFeatureIdentityMismatch);
  mate.feature_id = main.feature_id;
  mate.timestamp = std::nextafter(main.timestamp,
                                  std::numeric_limits<double>::infinity());
  EXPECT_EQ(TurnSafeCertificate::TargetTimeStereoRange(
                main_camera, main, mate_camera, mate)
                .status,
            TurnSafeCertificateStatus::kTimestampIdentityMismatch);

  mate.timestamp = main.timestamp;
  mate.raw_pixel = main.raw_pixel;
  mate.captured_normalized = main.captured_normalized;
  EXPECT_EQ(TurnSafeCertificate::TargetTimeStereoRange(
                main_camera, main, mate_camera, mate)
                .status,
            TurnSafeCertificateStatus::kStereoGeometryIllConditioned);
}

TEST(TurnSafeCertificateStereoRange,
     FrozenOneSidedLcbHasDeterministicMonteCarloCoverage) {
  const TurnSafeCertificateCamera main_camera =
      camera(0U, Eigen::Vector3d::Zero());
  const TurnSafeCertificateCamera mate_camera =
      camera(1U, Eigen::Vector3d(0.20, 0.0, 0.0));
  const Eigen::Vector3d point(0.28, -0.09, 4.5);
  const TurnSafeCertificateObservation main =
      observation_for_point(main_camera, point);
  const TurnSafeCertificateObservation mate =
      observation_for_point(mate_camera, point);
  const double true_range = point.norm();

  std::mt19937_64 generator(UINT64_C(0x72616e67656c6362));
  std::normal_distribution<double> noise(
      0.0, TurnSafeCertificate::kPixelNoiseSigma);
  constexpr int samples = 16000;
  int covered = 0;
  for (int sample = 0; sample < samples; ++sample) {
    const Eigen::Vector2d main_raw =
        main.raw_pixel + Eigen::Vector2d(noise(generator), noise(generator));
    const Eigen::Vector2d mate_raw =
        mate.raw_pixel + Eigen::Vector2d(noise(generator), noise(generator));
    const TurnSafeCertificateObservation noisy_main =
        observation_for_raw(main_camera, main_raw, main);
    const TurnSafeCertificateObservation noisy_mate =
        observation_for_raw(mate_camera, mate_raw, mate);
    const auto result = TurnSafeCertificate::TargetTimeStereoRange(
        main_camera, noisy_main, mate_camera, noisy_mate);
    ASSERT_TRUE(result.available())
        << "sample=" << sample << " status="
        << ov_msckf::turnsafe_certificate_status_name(result.status);
    if (true_range >= result.d_lcb) ++covered;
  }
  const double coverage =
      static_cast<double>(covered) / static_cast<double>(samples);
  RecordProperty("monte_carlo_samples", samples);
  RecordProperty("covered_samples", covered);
  RecordProperty("empirical_coverage", std::to_string(coverage));
  EXPECT_GE(coverage,
            TurnSafeCertificate::kJointUnionBoundConfidence);
  EXPECT_NEAR(coverage,
              TurnSafeCertificate::kRangeComponentConfidence, 1.5e-3);
}

TEST(TurnSafeCertificateTranslation,
     LeverArmJacobianMatchesOpenVinsLeftPerturbationFiniteDifferences) {
  TurnSafeCertificateCamera calibration =
      camera(0U, Eigen::Vector3d(0.08, -0.03, 0.02));
  calibration.R_ItoC =
      rotation(Eigen::Vector3d(0.3, -0.4, 0.2), 0.24);
  calibration.p_IinC =
      -calibration.R_ItoC * Eigen::Vector3d(0.08, -0.03, 0.02);
  TurnSafeCertificateClonePose source;
  source.R_GtoI = rotation(Eigen::Vector3d(0.2, 0.7, -0.1), 0.41);
  source.p_IinG << 1.0, -0.4, 0.3;
  TurnSafeCertificateClonePose target;
  target.R_GtoI = rotation(Eigen::Vector3d(-0.3, 0.2, 0.8), -0.28);
  target.p_IinG << 0.7, -0.1, 0.5;

  const auto result = TurnSafeCertificate::RelativeCameraTranslation(
      calibration, source, target, dense_pair_covariance());
  ASSERT_TRUE(result.available())
      << ov_msckf::turnsafe_certificate_status_name(result.status);
  constexpr double epsilon = 1.0e-7;
  Eigen::Matrix<double, 3, 12> finite_difference;
  for (Eigen::Index column = 0; column < 12; ++column) {
    TurnSafeCertificateClonePose source_plus = source;
    TurnSafeCertificateClonePose source_minus = source;
    TurnSafeCertificateClonePose target_plus = target;
    TurnSafeCertificateClonePose target_minus = target;
    const Eigen::Index local_column = column % 6;
    Eigen::Vector3d axis = Eigen::Vector3d::Zero();
    if (local_column < 3) axis(local_column) = 1.0;
    if (column < 6) {
      if (local_column < 3) {
        source_plus.R_GtoI =
            rotation(axis, -epsilon) * source.R_GtoI;
        source_minus.R_GtoI =
            rotation(axis, epsilon) * source.R_GtoI;
      } else {
        source_plus.p_IinG(local_column - 3) += epsilon;
        source_minus.p_IinG(local_column - 3) -= epsilon;
      }
    } else {
      if (local_column < 3) {
        target_plus.R_GtoI =
            rotation(axis, -epsilon) * target.R_GtoI;
        target_minus.R_GtoI =
            rotation(axis, epsilon) * target.R_GtoI;
      } else {
        target_plus.p_IinG(local_column - 3) += epsilon;
        target_minus.p_IinG(local_column - 3) -= epsilon;
      }
    }
    finite_difference.col(column) =
        (relative_camera_center_mean(calibration, source_plus, target_plus) -
         relative_camera_center_mean(calibration, source_minus,
                                     target_minus)) /
        (2.0 * epsilon);
  }
  EXPECT_LE((result.translation_jacobian - finite_difference).norm(),
            3.0e-9);
}

TEST(TurnSafeCertificateTranslation,
     AllCrossBlocksAndLeverArmEnterInflatedCovariance) {
  TurnSafeCertificateCamera calibration =
      camera(0U, Eigen::Vector3d(0.12, -0.06, 0.04));
  TurnSafeCertificateClonePose source;
  source.R_GtoI = rotation(Eigen::Vector3d::UnitZ(), 0.3);
  source.p_IinG << 0.4, -0.2, 0.1;
  TurnSafeCertificateClonePose target;
  target.R_GtoI = rotation(Eigen::Vector3d::UnitY(), -0.2);
  target.p_IinG << 0.1, 0.3, -0.2;
  const TurnSafeCapturedPairCovariance covariance = dense_pair_covariance();
  const auto dense = TurnSafeCertificate::RelativeCameraTranslation(
      calibration, source, target, covariance);
  ASSERT_TRUE(dense.available());
  EXPECT_LE((dense.translation_covariance -
             dense.translation_jacobian *
                 dense.captured_pair_covariance *
                 dense.translation_jacobian.transpose())
                .norm(),
            1.0e-18);
  EXPECT_LE((dense.certified_translation_covariance -
             2.0 * dense.translation_covariance)
                .norm(),
            1.0e-18);
  EXPECT_DOUBLE_EQ(dense.covariance_inflation, 2.0);

  TurnSafeCapturedPairCovariance without_cross = covariance;
  without_cross.P_st.setZero();
  without_cross.P_ts.setZero();
  const auto block_diagonal =
      TurnSafeCertificate::RelativeCameraTranslation(
          calibration, source, target, without_cross);
  ASSERT_TRUE(block_diagonal.available());
  EXPECT_GT((dense.translation_covariance -
             block_diagonal.translation_covariance)
                .norm(),
            1.0e-8);

  TurnSafeCertificateCamera zero_lever = calibration;
  zero_lever.p_IinC.setZero();
  const auto no_lever = TurnSafeCertificate::RelativeCameraTranslation(
      zero_lever, source, target, covariance);
  ASSERT_TRUE(no_lever.available());
  EXPECT_DOUBLE_EQ(
      (no_lever.translation_jacobian.block<3, 3>(0, 0).norm()), 0.0);
  EXPECT_DOUBLE_EQ(
      (no_lever.translation_jacobian.block<3, 3>(0, 6).norm()), 0.0);
}

TEST(TurnSafeCertificateTranslation,
     DeterministicTranslationNeesStressBindsInflatedCoverage) {
  TurnSafeCertificateCamera calibration =
      camera(0U, Eigen::Vector3d(0.12, -0.06, 0.04));
  TurnSafeCertificateClonePose source;
  source.R_GtoI = rotation(Eigen::Vector3d(0.2, 0.7, -0.1), 0.41);
  source.p_IinG << 1.0, -0.4, 0.3;
  TurnSafeCertificateClonePose target;
  target.R_GtoI = rotation(Eigen::Vector3d(-0.3, 0.2, 0.8), -0.28);
  target.p_IinG << 0.7, -0.1, 0.5;
  const auto certificate = TurnSafeCertificate::RelativeCameraTranslation(
      calibration, source, target, dense_pair_covariance());
  ASSERT_TRUE(certificate.available());

  Eigen::LLT<Eigen::Matrix<double, 12, 12>> pair_llt(
      certificate.captured_pair_covariance);
  Eigen::LLT<Eigen::Matrix3d> translation_llt(
      certificate.translation_covariance);
  Eigen::LLT<Eigen::Matrix3d> certified_llt(
      certificate.certified_translation_covariance);
  ASSERT_EQ(pair_llt.info(), Eigen::Success);
  ASSERT_EQ(translation_llt.info(), Eigen::Success);
  ASSERT_EQ(certified_llt.info(), Eigen::Success);

  std::mt19937_64 generator(UINT64_C(0x6e65657373747273));
  std::normal_distribution<double> normal(0.0, 1.0);
  constexpr int samples = 24000;
  double uninflated_sum = 0.0;
  double certified_sum = 0.0;
  int certified_covered = 0;
  for (int sample = 0; sample < samples; ++sample) {
    Eigen::Matrix<double, 12, 1> standard;
    for (Eigen::Index index = 0; index < standard.rows(); ++index)
      standard(index) = normal(generator);
    const Eigen::Matrix<double, 12, 1> state_error =
        pair_llt.matrixL() * standard;
    const Eigen::Vector3d translation_error =
        certificate.translation_jacobian * state_error;
    const double uninflated_nees = translation_error.dot(
        translation_llt.solve(translation_error));
    const double certified_nees = translation_error.dot(
        certified_llt.solve(translation_error));
    ASSERT_TRUE(std::isfinite(uninflated_nees));
    ASSERT_TRUE(std::isfinite(certified_nees));
    uninflated_sum += uninflated_nees;
    certified_sum += certified_nees;
    if (certified_nees <= certificate.chi_square_quantile)
      ++certified_covered;
  }
  const double uninflated_mean = uninflated_sum / samples;
  const double certified_mean = certified_sum / samples;
  const double certified_coverage =
      static_cast<double>(certified_covered) / samples;
  RecordProperty("monte_carlo_samples", samples);
  RecordProperty("uninflated_nees_mean", std::to_string(uninflated_mean));
  RecordProperty("certified_nees_mean", std::to_string(certified_mean));
  RecordProperty("certified_covered_samples", certified_covered);
  RecordProperty("certified_empirical_coverage",
                 std::to_string(certified_coverage));
  EXPECT_NEAR(uninflated_mean, 3.0, 0.06);
  EXPECT_NEAR(certified_mean, 1.5, 0.03);
  EXPECT_GE(certified_coverage,
            TurnSafeCertificate::kTranslationComponentConfidence);
}

TEST(TurnSafeCertificateTranslation,
     AsymmetricAndNonPsdCapturedCovariancesFailWithoutRepair) {
  const TurnSafeCertificateCamera calibration =
      camera(0U, Eigen::Vector3d(0.1, 0.0, 0.0));
  TurnSafeCertificateClonePose source;
  source.R_GtoI.setIdentity();
  source.p_IinG.setZero();
  TurnSafeCertificateClonePose target = source;
  target.p_IinG(0) = 0.1;

  TurnSafeCapturedPairCovariance asymmetric = dense_pair_covariance();
  asymmetric.P_ts(0, 0) += 1.0e-6;
  EXPECT_EQ(TurnSafeCertificate::RelativeCameraTranslation(
                calibration, source, target, asymmetric)
                .status,
            TurnSafeCertificateStatus::kCovarianceSymmetryFailure);

  TurnSafeCapturedPairCovariance indefinite;
  indefinite.P_ss.setZero();
  indefinite.P_tt.setZero();
  indefinite.P_st.setZero();
  indefinite.P_ts.setZero();
  indefinite.P_ss(0, 0) = -1.0;
  EXPECT_EQ(TurnSafeCertificate::RelativeCameraTranslation(
                calibration, source, target, indefinite)
                .status,
            TurnSafeCertificateStatus::kCovarianceNotPsd);
}

TEST(TurnSafeCertificateResidualCovariance,
     ExactBearingNoisePropagationMatchesDeterministicMonteCarlo) {
  const TurnSafeCertificateCamera calibration =
      camera(0U, Eigen::Vector3d::Zero());
  const Eigen::Vector2d source_normalized(-0.12, 0.08);
  const Eigen::Vector2d target_normalized(0.16, -0.05);
  const Eigen::Vector2d source_raw =
      raw_for_normalized(calibration, source_normalized);
  const Eigen::Vector2d target_raw =
      raw_for_normalized(calibration, target_normalized);
  const TurnSafeBearingResult source =
      TurnSafeCertificate::BearingFromCapturedNormalized(
          calibration, source_raw, source_normalized);
  const TurnSafeBearingResult target =
      TurnSafeCertificate::BearingFromCapturedNormalized(
          calibration, target_raw, target_normalized);
  ASSERT_TRUE(source.available());
  ASSERT_TRUE(target.available());
  const Eigen::Matrix3d relative_rotation =
      rotation(Eigen::Vector3d(0.2, 0.5, -0.1), 0.18);
  const Eigen::Matrix<double, 3, 2> basis = tangent_basis(target.bearing);
  const auto covariance =
      TurnSafeCertificate::BearingResidualCovariance(
          source, target, relative_rotation, basis);
  ASSERT_TRUE(covariance.available());

  std::mt19937_64 generator(UINT64_C(0x6365727469666963));
  std::normal_distribution<double> noise(
      0.0, TurnSafeCertificate::kPixelNoiseSigma);
  Eigen::Vector2d empirical_mean = Eigen::Vector2d::Zero();
  Eigen::Matrix2d empirical_second = Eigen::Matrix2d::Zero();
  constexpr int samples = 16000;
  for (int sample = 0; sample < samples; ++sample) {
    const Eigen::Vector3d noisy_source = bearing_for_raw(
        calibration,
        source_raw + Eigen::Vector2d(noise(generator), noise(generator)));
    const Eigen::Vector3d noisy_target = bearing_for_raw(
        calibration,
        target_raw + Eigen::Vector2d(noise(generator), noise(generator)));
    const Eigen::Vector2d residual =
        basis.transpose() *
        (noisy_target - relative_rotation * noisy_source);
    empirical_mean += residual;
    empirical_second.noalias() += residual * residual.transpose();
  }
  empirical_mean /= static_cast<double>(samples);
  const Eigen::Matrix2d empirical =
      empirical_second / static_cast<double>(samples) -
      empirical_mean * empirical_mean.transpose();
  const double relative_error =
      (empirical - covariance.covariance).norm() /
      covariance.covariance.norm();
  RecordProperty("monte_carlo_samples", samples);
  RecordProperty("relative_covariance_error", std::to_string(relative_error));
  EXPECT_LE(relative_error, 0.04);
}

TEST(TurnSafeCertificateAcute,
     StrictBoundaryWorstAxisAndInclusiveRhoAreFrozen) {
  Eigen::Matrix2d covariance;
  covariance << 4.0, 0.0, 0.0, 9.0;
  const auto equal = TurnSafeCertificate::AcuteAndRho(10.0, 10.0,
                                                       covariance);
  EXPECT_EQ(equal.status,
            TurnSafeCertificateStatus::kTranslationNotAcute);
  EXPECT_FALSE(equal.theta_available);
  EXPECT_FALSE(equal.rho_available);
  EXPECT_EQ(TurnSafeCertificate::AcuteAndRho(10.1, 10.0, covariance).status,
            TurnSafeCertificateStatus::kTranslationNotAcute);

  const auto acute =
      TurnSafeCertificate::AcuteAndRho(5.0, 10.0, covariance);
  ASSERT_TRUE(acute.admitted());
  EXPECT_DOUBLE_EQ(acute.lambda_min, 4.0);
  EXPECT_DOUBLE_EQ(acute.lambda_max, 9.0);
  EXPECT_NEAR(acute.rho_trans, std::asin(0.5) / 2.0, 1.0e-15);

  const Eigen::Matrix2d unit_covariance = Eigen::Matrix2d::Identity();
  const auto inclusive = TurnSafeCertificate::AcuteAndRho(
      std::sin(0.5), 1.0, unit_covariance);
  EXPECT_TRUE(inclusive.admitted());
  EXPECT_LE(inclusive.rho_trans, 0.5);
  const auto exceeded = TurnSafeCertificate::AcuteAndRho(
      std::sin(0.5001), 1.0, unit_covariance);
  EXPECT_EQ(exceeded.status, TurnSafeCertificateStatus::kRhoExceeded);

  Eigen::Matrix2d ill_conditioned = Eigen::Matrix2d::Zero();
  ill_conditioned(0, 0) = 0.5e-12;
  ill_conditioned(1, 1) = 1.0;
  EXPECT_EQ(TurnSafeCertificate::AcuteAndRho(0.1, 1.0,
                                             ill_conditioned)
                .status,
            TurnSafeCertificateStatus::kResidualCovarianceIllConditioned);
}

TEST(TurnSafeCertificateCoverage,
     FrozenComponentQuantilesAndUnionBoundMeetDeclaredCoverage) {
  constexpr std::size_t samples = 100000U;
  const boost::math::normal_distribution<double> normal;
  const boost::math::chi_squared_distribution<double> chi_squared(3.0);
  const double range_quantile = boost::math::quantile(
      normal, TurnSafeCertificate::kRangeComponentConfidence);
  const double translation_quantile = boost::math::quantile(
      chi_squared, TurnSafeCertificate::kTranslationComponentConfidence);
  std::size_t range_covered = 0U;
  std::size_t translation_covered = 0U;
  std::size_t jointly_covered = 0U;
  const double alpha =
      1.0 - TurnSafeCertificate::kRangeComponentConfidence;
  for (std::size_t sample = 0U; sample < samples; ++sample) {
    const double probability =
        (static_cast<double>(sample) + 0.5) /
        static_cast<double>(samples);
    const bool range_pass =
        boost::math::quantile(normal, probability) <= range_quantile;
    const bool translation_pass =
        boost::math::quantile(chi_squared, probability) <=
        translation_quantile;
    if (range_pass) ++range_covered;
    if (translation_pass) ++translation_covered;

    // Adversarial disjoint component-failure sets attain the union bound.
    const bool union_range_pass = probability >= alpha;
    const bool union_translation_pass = probability < 1.0 - alpha;
    if (union_range_pass && union_translation_pass) ++jointly_covered;
  }
  const double range_coverage =
      static_cast<double>(range_covered) / static_cast<double>(samples);
  const double translation_coverage =
      static_cast<double>(translation_covered) /
      static_cast<double>(samples);
  const double joint_coverage =
      static_cast<double>(jointly_covered) / static_cast<double>(samples);
  RecordProperty("deterministic_grid_samples",
                 static_cast<int>(samples));
  RecordProperty("range_component_covered",
                 static_cast<int>(range_covered));
  RecordProperty("translation_component_covered",
                 static_cast<int>(translation_covered));
  RecordProperty("joint_union_bound_covered",
                 static_cast<int>(jointly_covered));
  RecordProperty("range_component_coverage",
                 std::to_string(range_coverage));
  RecordProperty("translation_component_coverage",
                 std::to_string(translation_coverage));
  RecordProperty("joint_union_bound_coverage",
                 std::to_string(joint_coverage));
  EXPECT_GE(range_coverage,
            TurnSafeCertificate::kRangeComponentConfidence);
  EXPECT_GE(translation_coverage,
            TurnSafeCertificate::kTranslationComponentConfidence);
  EXPECT_GE(joint_coverage,
            TurnSafeCertificate::kJointUnionBoundConfidence);
  EXPECT_NEAR(joint_coverage, 1.0 - 2.0 * alpha, 1.0e-12);
}

} // namespace
