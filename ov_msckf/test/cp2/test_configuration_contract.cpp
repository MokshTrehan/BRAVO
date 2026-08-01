/*
 * SchurVIO-Lite CP2 configuration-contract tests.
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include "core/VioManagerOptions.h"
#include "types/LandmarkRepresentation.h"
#include "update/SchurUpdate.h"
#include "update/UpdaterOptions.h"
#include "utils/opencv_yaml_parse.h"

#include <gtest/gtest.h>

#include <boost/filesystem.hpp>

#include <array>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <limits>
#include <memory>
#include <string>
#include <unistd.h>

namespace {

class TemporaryYaml {
public:
  explicit TemporaryYaml(const std::string &body)
      : path_((boost::filesystem::temp_directory_path() /
               boost::filesystem::unique_path("schurvio_cp2_config_%%%%-%%%%-%%%%.yaml"))
                  .string()) {
    std::ofstream stream(path_, std::ios::out | std::ios::trunc);
    stream << "%YAML:1.0\n---\n"
           << "up_msckf_sigma_px: 1.0\n"
           << "up_msckf_chi2_multipler: 1.0\n"
           << body;
    stream.close();
    EXPECT_TRUE(stream.good());
  }

  ~TemporaryYaml() {
    boost::system::error_code error;
    boost::filesystem::remove(path_, error);
  }

  const std::string &path() const { return path_; }

private:
  std::string path_;
};

struct LoadedMode {
  ov_msckf::UpdaterOptions::LandmarkElimination mode;
  double variance;
  bool parser_successful;
};

LoadedMode load_mode_through_production_seam(const std::string &path) {
  auto parser = std::make_shared<ov_core::YamlParser>(path);
  ov_msckf::VioManagerOptions options;
  options.load_and_validate_msckf_update_configuration(parser);
  return {options.msckf_options.landmark_elimination, options.msckf_options.sigma_pix_sq, parser->successful()};
}

// OpenVINS deliberately sends every PRINT_* level to stdout. GoogleTest death
// tests match stderr, so route the child process's stdout before exercising a
// fatal startup path. This lets each assertion protect the classified reason,
// not merely the fact that some exit occurred.
void route_openvins_output_to_death_test_stderr() {
  std::fflush(stdout);
  if (::dup2(STDERR_FILENO, STDOUT_FILENO) == -1) {
    std::_Exit(120);
  }
}

void expect_zero_repair_and_fallback_counters(const ov_msckf::SchurReductionResult &result) {
  EXPECT_EQ(result.jitter_count, 0U);
  EXPECT_EQ(result.clamp_count, 0U);
  EXPECT_EQ(result.regularization_count, 0U);
  EXPECT_EQ(result.fallback_count, 0U);
}

TEST(CP2Configuration, OptionalModeUsesDefaultAndExactExplicitSpellings) {
  const ov_msckf::UpdaterOptions updater_defaults;
  EXPECT_EQ(updater_defaults.landmark_elimination, ov_msckf::UpdaterOptions::LandmarkElimination::NULLSPACE);

  const TemporaryYaml absent_key("unrelated_optional_test_key: 1\n");
  const LoadedMode absent = load_mode_through_production_seam(absent_key.path());
  EXPECT_TRUE(absent.parser_successful);
  EXPECT_EQ(absent.mode, ov_msckf::UpdaterOptions::LandmarkElimination::NULLSPACE);
  EXPECT_DOUBLE_EQ(absent.variance, 1.0);

  const TemporaryYaml explicit_nullspace("up_msckf_landmark_elimination: nullspace\n");
  const LoadedMode nullspace = load_mode_through_production_seam(explicit_nullspace.path());
  EXPECT_TRUE(nullspace.parser_successful);
  EXPECT_EQ(nullspace.mode, ov_msckf::UpdaterOptions::LandmarkElimination::NULLSPACE);

  const TemporaryYaml explicit_schur("up_msckf_landmark_elimination: schur\n");
  const LoadedMode schur = load_mode_through_production_seam(explicit_schur.path());
  EXPECT_TRUE(schur.parser_successful);
  EXPECT_EQ(schur.mode, ov_msckf::UpdaterOptions::LandmarkElimination::SCHUR);
  EXPECT_NE(schur.mode, nullspace.mode);
}

TEST(CP2Configuration, InvalidSpellingsCannotMutateASelectedModeOrFallBack) {
  using Elimination = ov_msckf::UpdaterOptions::LandmarkElimination;

  Elimination selected = Elimination::SCHUR;
  EXPECT_FALSE(ov_msckf::UpdaterOptions::try_landmark_elimination_from_string("unknown", selected));
  EXPECT_EQ(selected, Elimination::SCHUR);

  selected = Elimination::NULLSPACE;
  EXPECT_FALSE(ov_msckf::UpdaterOptions::try_landmark_elimination_from_string("fixed_two_pass", selected));
  EXPECT_EQ(selected, Elimination::NULLSPACE);

  // The public spellings are exact and case-sensitive.
  EXPECT_FALSE(ov_msckf::UpdaterOptions::try_landmark_elimination_from_string("SCHUR", selected));
  EXPECT_EQ(selected, Elimination::NULLSPACE);
}

TEST(CP2Configuration, UnknownAndFixedTwoPassSpellingsFailStartup) {
  const TemporaryYaml unknown("up_msckf_landmark_elimination: unknown\n");
  EXPECT_EXIT(
      {
        route_openvins_output_to_death_test_stderr();
        (void)load_mode_through_production_seam(unknown.path());
        std::exit(EXIT_SUCCESS);
      },
      ::testing::ExitedWithCode(EXIT_FAILURE), "invalid MSCKF landmark elimination mode");

  const TemporaryYaml fixed_two_pass("up_msckf_landmark_elimination: fixed_two_pass\n");
  EXPECT_EXIT(
      {
        route_openvins_output_to_death_test_stderr();
        (void)load_mode_through_production_seam(fixed_two_pass.path());
        std::exit(EXIT_SUCCESS);
      },
      ::testing::ExitedWithCode(EXIT_FAILURE), "invalid MSCKF landmark elimination mode");

  // A present non-string value must not be confused with the absent optional
  // key and therefore must not inherit the nullspace default.
  const TemporaryYaml numeric_value("up_msckf_landmark_elimination: 7\n");
  EXPECT_EXIT(
      {
        route_openvins_output_to_death_test_stderr();
        (void)load_mode_through_production_seam(numeric_value.path());
        std::exit(EXIT_SUCCESS);
      },
      ::testing::ExitedWithCode(EXIT_FAILURE), "invalid MSCKF landmark elimination mode");

  const TemporaryYaml sequence_value("up_msckf_landmark_elimination: [ schur ]\n");
  EXPECT_EXIT(
      {
        route_openvins_output_to_death_test_stderr();
        (void)load_mode_through_production_seam(sequence_value.path());
        std::exit(EXIT_SUCCESS);
      },
      ::testing::ExitedWithCode(EXIT_FAILURE), "invalid MSCKF landmark elimination mode");
}

TEST(CP2Configuration, SchurRequiresGlobal3DWhileNullspaceRetainsExistingRepresentations) {
  ov_msckf::VioManagerOptions baseline;
  baseline.msckf_options.landmark_elimination = ov_msckf::UpdaterOptions::LandmarkElimination::NULLSPACE;
  baseline.state_options.feat_rep_msckf =
      ov_type::LandmarkRepresentation::Representation::ANCHORED_MSCKF_INVERSE_DEPTH;
  baseline.validate_msckf_update_configuration_or_exit();
  EXPECT_DOUBLE_EQ(baseline.msckf_options.sigma_pix_sq, 1.0);

  ov_msckf::VioManagerOptions candidate;
  candidate.msckf_options.landmark_elimination = ov_msckf::UpdaterOptions::LandmarkElimination::SCHUR;
  candidate.state_options.feat_rep_msckf = ov_type::LandmarkRepresentation::Representation::GLOBAL_3D;
  candidate.validate_msckf_update_configuration_or_exit();
  EXPECT_DOUBLE_EQ(candidate.msckf_options.sigma_pix_sq, 1.0);

  EXPECT_EXIT(
      {
        route_openvins_output_to_death_test_stderr();
        ov_msckf::VioManagerOptions options;
        options.msckf_options.landmark_elimination = ov_msckf::UpdaterOptions::LandmarkElimination::SCHUR;
        options.state_options.feat_rep_msckf =
            ov_type::LandmarkRepresentation::Representation::ANCHORED_MSCKF_INVERSE_DEPTH;
        options.print_and_load_noise();
        std::exit(EXIT_SUCCESS);
      },
      ::testing::ExitedWithCode(EXIT_FAILURE), "requires feat_rep_msckf=GLOBAL_3D");
}

TEST(CP2Configuration, NonfiniteAndNonpositiveSigmaFailBeforeVarianceMaterialization) {
  const std::array<double, 6> invalid_sigma{{0.0,
                                             -std::numeric_limits<double>::denorm_min(),
                                             -1.0,
                                             std::numeric_limits<double>::infinity(),
                                             -std::numeric_limits<double>::infinity(),
                                             std::numeric_limits<double>::quiet_NaN()}};
  for (double sigma : invalid_sigma) {
    SCOPED_TRACE(::testing::Message() << "sigma=" << sigma);
    EXPECT_EXIT(
        {
          route_openvins_output_to_death_test_stderr();
          ov_msckf::VioManagerOptions options;
          options.msckf_options.sigma_pix = sigma;
          options.print_and_load_noise();
          std::exit(EXIT_SUCCESS);
        },
        ::testing::ExitedWithCode(EXIT_FAILURE), "must be finite and strictly positive");
  }
}

TEST(CP2Configuration, UnderflowingAndOverflowingVarianceFailStartup) {
  const double underflowing_sigma = std::numeric_limits<double>::denorm_min();
  ASSERT_GT(underflowing_sigma, 0.0);
  ASSERT_TRUE(std::isfinite(underflowing_sigma));
  ASSERT_DOUBLE_EQ(underflowing_sigma * underflowing_sigma, 0.0);

  EXPECT_EXIT(
      {
        route_openvins_output_to_death_test_stderr();
        ov_msckf::VioManagerOptions options;
        options.msckf_options.sigma_pix = underflowing_sigma;
        options.print_and_load_noise();
        std::exit(EXIT_SUCCESS);
      },
      ::testing::ExitedWithCode(EXIT_FAILURE), "squared must be finite and strictly positive");

  const double overflowing_sigma = std::numeric_limits<double>::max();
  ASSERT_TRUE(std::isfinite(overflowing_sigma));
  ASSERT_TRUE(std::isinf(overflowing_sigma * overflowing_sigma));
  EXPECT_EXIT(
      {
        route_openvins_output_to_death_test_stderr();
        ov_msckf::VioManagerOptions options;
        options.msckf_options.sigma_pix = overflowing_sigma;
        options.print_and_load_noise();
        std::exit(EXIT_SUCCESS);
      },
      ::testing::ExitedWithCode(EXIT_FAILURE), "squared must be finite and strictly positive");
}

TEST(CP2Configuration, PositiveRepresentableVarianceIsMaterializedExactly) {
  ov_msckf::VioManagerOptions tiny;
  tiny.msckf_options.sigma_pix = std::sqrt(std::numeric_limits<double>::denorm_min());
  tiny.msckf_options.sigma_pix_sq = -1.0;
  ASSERT_TRUE(std::isfinite(tiny.msckf_options.sigma_pix));
  ASSERT_GT(tiny.msckf_options.sigma_pix * tiny.msckf_options.sigma_pix, 0.0);
  tiny.validate_msckf_update_configuration_or_exit();
  EXPECT_DOUBLE_EQ(tiny.msckf_options.sigma_pix_sq,
                   tiny.msckf_options.sigma_pix * tiny.msckf_options.sigma_pix);

  ov_msckf::VioManagerOptions large;
  large.msckf_options.sigma_pix = std::sqrt(std::numeric_limits<double>::max()) / 2.0;
  large.msckf_options.sigma_pix_sq = -1.0;
  ASSERT_TRUE(std::isfinite(large.msckf_options.sigma_pix * large.msckf_options.sigma_pix));
  large.validate_msckf_update_configuration_or_exit();
  EXPECT_DOUBLE_EQ(large.msckf_options.sigma_pix_sq,
                   large.msckf_options.sigma_pix * large.msckf_options.sigma_pix);
}

TEST(CP2Configuration, InvalidEnumFailsStartupWithoutSelectingAStringMode) {
  using Elimination = ov_msckf::UpdaterOptions::LandmarkElimination;
  const Elimination invalid_mode = static_cast<Elimination>(std::numeric_limits<int>::max());
  EXPECT_FALSE(ov_msckf::UpdaterOptions::landmark_elimination_is_supported(invalid_mode));
  EXPECT_EQ(ov_msckf::UpdaterOptions::landmark_elimination_as_string(invalid_mode), "unknown");

  EXPECT_EXIT(
      {
        route_openvins_output_to_death_test_stderr();
        ov_msckf::VioManagerOptions options;
        options.msckf_options.landmark_elimination = invalid_mode;
        options.print_and_load_noise();
        std::exit(EXIT_SUCCESS);
      },
      ::testing::ExitedWithCode(EXIT_FAILURE), "invalid MSCKF landmark elimination mode: unknown");
}

TEST(CP2Configuration, RuntimeInvalidSigmaNeverInvokesRepairOrSilentFallback) {
  Eigen::MatrixXd H_x = Eigen::MatrixXd::Zero(5, 4);
  Eigen::MatrixXd H_f = Eigen::MatrixXd::Zero(5, 3);
  H_f.topRows(3).setIdentity();
  const Eigen::VectorXd residual = Eigen::VectorXd::Zero(5);
  const std::array<double, 4> invalid_sigma{{0.0,
                                             -1.0,
                                             std::numeric_limits<double>::infinity(),
                                             std::numeric_limits<double>::quiet_NaN()}};

  for (double sigma : invalid_sigma) {
    SCOPED_TRACE(::testing::Message() << "sigma=" << sigma);
    const ov_msckf::SchurReductionResult result = ov_msckf::SchurUpdate::Reduce(H_x, H_f, residual, sigma);
    EXPECT_FALSE(result.accepted());
    EXPECT_EQ(result.status, ov_msckf::SchurReductionStatus::kNonfinite);
    EXPECT_EQ(result.stage, ov_msckf::SchurReductionStage::kSigma);
    expect_zero_repair_and_fallback_counters(result);
  }
}

} // namespace
