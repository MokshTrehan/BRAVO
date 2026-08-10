// SPDX-License-Identifier: GPL-3.0-or-later

#include <gtest/gtest.h>

#include <boost/filesystem.hpp>
#include <cstdlib>
#include <fstream>
#include <limits>
#include <memory>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>
#include <unistd.h>

#include "core/VioManagerOptions.h"
#include "update/UpdaterOptions.h"
#include "utils/opencv_yaml_parse.h"

namespace {

using ov_core::YamlParser;
using ov_msckf::UpdaterOptions;
using ov_msckf::VioManagerOptions;

class TemporaryYaml {
public:
  explicit TemporaryYaml(const std::string &body)
      : path_(boost::filesystem::temp_directory_path() /
              boost::filesystem::unique_path("schurvio-visual-pass-%%%%-%%%%-%%%%.yaml")) {
    std::ofstream output(path_.string(), std::ios::out | std::ios::trunc);
    if (!output.is_open()) {
      throw std::runtime_error("unable to create temporary YAML configuration");
    }
    output << "%YAML:1.0\n---\n" << body;
    output.close();
    if (!output) {
      throw std::runtime_error("unable to write temporary YAML configuration");
    }
  }

  ~TemporaryYaml() {
    boost::system::error_code ignored;
    boost::filesystem::remove(path_, ignored);
  }

  TemporaryYaml(const TemporaryYaml &) = delete;
  TemporaryYaml &operator=(const TemporaryYaml &) = delete;

  const std::string path() const { return path_.string(); }

private:
  boost::filesystem::path path_;
};

std::string full_msckf_yaml(const std::string &pass_value, const std::string &elimination) {
  return "up_msckf_sigma_px: 1.0\n"
         "up_msckf_chi2_multipler: 5.0\n"
         "up_msckf_landmark_elimination: " +
         elimination + "\nup_msckf_max_visual_passes: " + pass_value + "\n";
}

std::shared_ptr<YamlParser> parser_for(const TemporaryYaml &yaml) {
  return std::make_shared<YamlParser>(yaml.path());
}

void expect_yaml_startup_rejection(const std::string &pass_value, const std::string &message_regex) {
  const TemporaryYaml yaml(full_msckf_yaml(pass_value, "schur"));
  EXPECT_EXIT(
      {
        (void)::dup2(STDERR_FILENO, STDOUT_FILENO);
        const auto parser = std::make_shared<YamlParser>(yaml.path());
        VioManagerOptions options;
        options.load_and_validate_msckf_update_configuration(parser);
        std::exit(EXIT_SUCCESS);
      },
      ::testing::ExitedWithCode(EXIT_FAILURE), message_regex);
}

TEST(CP1VisualPassConfig, DefaultIsExactlyOneAndOnlyOneOrTwoAreSupported) {
  const UpdaterOptions options;
  EXPECT_EQ(options.max_visual_passes, 1);
  EXPECT_FALSE(options.capture_conditioning_systems);
  EXPECT_TRUE(options.conditioning_capture_path.empty());
  EXPECT_TRUE(options.conditioning_capture_config_path.empty());
  EXPECT_FALSE(options.capture_update_envelopes_v2);
  EXPECT_TRUE(options.update_envelope_capture_path.empty());
  EXPECT_TRUE(options.update_envelope_run_id.empty());
  EXPECT_TRUE(options.update_envelope_sequence_id.empty());
  EXPECT_TRUE(options.update_envelope_capture_config_path.empty());
  EXPECT_TRUE(UpdaterOptions::max_visual_passes_is_supported(1));
  EXPECT_TRUE(UpdaterOptions::max_visual_passes_is_supported(2));
  EXPECT_FALSE(UpdaterOptions::max_visual_passes_is_supported(0));
  EXPECT_FALSE(UpdaterOptions::max_visual_passes_is_supported(-1));
  EXPECT_FALSE(UpdaterOptions::max_visual_passes_is_supported(3));
  EXPECT_FALSE(UpdaterOptions::max_visual_passes_is_supported(std::numeric_limits<int>::max()));
}

TEST(CP1VisualPassConfig, Schema2CaptureParsesOnlyWhenExplicitlyEnabled) {
  const TemporaryYaml yaml(
      full_msckf_yaml("1", "schur") +
      "up_msckf_capture_update_envelopes_v2: true\n"
      "up_msckf_update_envelope_capture_path: /tmp/schurvio-schema2-test.bin\n"
      "up_msckf_update_envelope_run_id: unit-run\n"
      "up_msckf_update_envelope_sequence_id: fixture-sequence\n");
  const auto parser = parser_for(yaml);
  VioManagerOptions options;
  options.load_and_validate_msckf_update_configuration(parser);

  EXPECT_TRUE(options.msckf_options.capture_update_envelopes_v2);
  EXPECT_EQ(options.msckf_options.update_envelope_capture_path,
            "/tmp/schurvio-schema2-test.bin");
  EXPECT_EQ(options.msckf_options.update_envelope_run_id, "unit-run");
  EXPECT_EQ(options.msckf_options.update_envelope_sequence_id,
            "fixture-sequence");
  EXPECT_EQ(options.msckf_options.update_envelope_capture_config_path,
            yaml.path());
}

TEST(CP1VisualPassConfig,
     Schema2CaptureRejectsUnsupportedOrIncompleteConfigurations) {
  const TemporaryYaml two_pass(
      full_msckf_yaml("2", "schur") +
      "up_msckf_capture_update_envelopes_v2: true\n"
      "up_msckf_update_envelope_capture_path: /tmp/schurvio-schema2-test.bin\n"
      "up_msckf_update_envelope_run_id: unit-run\n"
      "up_msckf_update_envelope_sequence_id: fixture-sequence\n");
  EXPECT_EXIT(
      {
        (void)::dup2(STDERR_FILENO, STDOUT_FILENO);
        const auto parser = std::make_shared<YamlParser>(two_pass.path());
        VioManagerOptions options;
        options.load_and_validate_msckf_update_configuration(parser);
        std::exit(EXIT_SUCCESS);
      },
      ::testing::ExitedWithCode(EXIT_FAILURE), "supported only.*passes=1");

  const TemporaryYaml relative(
      full_msckf_yaml("1", "schur") +
      "up_msckf_capture_update_envelopes_v2: true\n"
      "up_msckf_update_envelope_capture_path: capture.bin\n"
      "up_msckf_update_envelope_run_id: unit-run\n"
      "up_msckf_update_envelope_sequence_id: fixture-sequence\n");
  EXPECT_EXIT(
      {
        (void)::dup2(STDERR_FILENO, STDOUT_FILENO);
        const auto parser = std::make_shared<YamlParser>(relative.path());
        VioManagerOptions options;
        options.load_and_validate_msckf_update_configuration(parser);
        std::exit(EXIT_SUCCESS);
      },
      ::testing::ExitedWithCode(EXIT_FAILURE), "nonempty absolute path");

  const TemporaryYaml missing_identity(
      full_msckf_yaml("1", "schur") +
      "up_msckf_capture_update_envelopes_v2: true\n"
      "up_msckf_update_envelope_capture_path: /tmp/schurvio-schema2-test.bin\n");
  EXPECT_EXIT(
      {
        (void)::dup2(STDERR_FILENO, STDOUT_FILENO);
        const auto parser =
            std::make_shared<YamlParser>(missing_identity.path());
        VioManagerOptions options;
        options.load_and_validate_msckf_update_configuration(parser);
        std::exit(EXIT_SUCCESS);
      },
      ::testing::ExitedWithCode(EXIT_FAILURE),
      "requires nonempty run and sequence IDs");

  const TemporaryYaml mutually_exclusive(
      full_msckf_yaml("1", "schur") +
      "up_msckf_capture_conditioning_systems: true\n"
      "up_msckf_conditioning_capture_path: /tmp/schurvio-schema1-test.bin\n"
      "up_msckf_capture_update_envelopes_v2: true\n"
      "up_msckf_update_envelope_capture_path: /tmp/schurvio-schema2-test.bin\n"
      "up_msckf_update_envelope_run_id: unit-run\n"
      "up_msckf_update_envelope_sequence_id: fixture-sequence\n");
  EXPECT_EXIT(
      {
        (void)::dup2(STDERR_FILENO, STDOUT_FILENO);
        const auto parser =
            std::make_shared<YamlParser>(mutually_exclusive.path());
        VioManagerOptions options;
        options.load_and_validate_msckf_update_configuration(parser);
        std::exit(EXIT_SUCCESS);
      },
      ::testing::ExitedWithCode(EXIT_FAILURE), "mutually exclusive");
}

TEST(CP1VisualPassConfig, ConditioningCaptureParsesOnlyWhenExplicitlyEnabled) {
  const TemporaryYaml yaml(
      full_msckf_yaml("1", "nullspace") +
      "up_msckf_capture_conditioning_systems: true\n"
      "up_msckf_conditioning_capture_path: /tmp/schurvio-conditioning-test.bin\n");
  const auto parser = parser_for(yaml);
  VioManagerOptions options;
  options.load_and_validate_msckf_update_configuration(parser);

  EXPECT_TRUE(options.msckf_options.capture_conditioning_systems);
  EXPECT_EQ(options.msckf_options.conditioning_capture_path,
            "/tmp/schurvio-conditioning-test.bin");
  EXPECT_EQ(options.msckf_options.conditioning_capture_config_path,
            yaml.path());
}

TEST(CP1VisualPassConfig, ConditioningCaptureRejectsTwoPassAndRelativeOutput) {
  const TemporaryYaml two_pass(
      full_msckf_yaml("2", "schur") +
      "up_msckf_capture_conditioning_systems: true\n"
      "up_msckf_conditioning_capture_path: /tmp/schurvio-conditioning-test.bin\n");
  EXPECT_EXIT(
      {
        (void)::dup2(STDERR_FILENO, STDOUT_FILENO);
        const auto parser = std::make_shared<YamlParser>(two_pass.path());
        VioManagerOptions options;
        options.load_and_validate_msckf_update_configuration(parser);
        std::exit(EXIT_SUCCESS);
      },
      ::testing::ExitedWithCode(EXIT_FAILURE), "supported only.*passes=1");

  const TemporaryYaml relative(
      full_msckf_yaml("1", "nullspace") +
      "up_msckf_capture_conditioning_systems: true\n"
      "up_msckf_conditioning_capture_path: capture.bin\n");
  EXPECT_EXIT(
      {
        (void)::dup2(STDERR_FILENO, STDOUT_FILENO);
        const auto parser = std::make_shared<YamlParser>(relative.path());
        VioManagerOptions options;
        options.load_and_validate_msckf_update_configuration(parser);
        std::exit(EXIT_SUCCESS);
      },
      ::testing::ExitedWithCode(EXIT_FAILURE), "nonempty absolute path");
}

TEST(CP1VisualPassConfig, MissingOptionalValuePreservesExistingValue) {
  const TemporaryYaml yaml("unrelated_value: 7\n");
  const auto parser = parser_for(yaml);
  int destination = 2;
  const YamlParser::OptionalExactIntResult parsed =
      parser->parse_optional_exact_int("up_msckf_max_visual_passes", destination);

  EXPECT_EQ(parsed.status, YamlParser::OptionalExactIntStatus::MISSING);
  EXPECT_EQ(parsed.source, YamlParser::OptionalExactIntSource::NONE);
  EXPECT_EQ(destination, 2);

  VioManagerOptions options;
  EXPECT_EQ(options.msckf_options.max_visual_passes, 1);
  options.load_msckf_max_visual_passes_or_exit(parser);
  EXPECT_EQ(options.msckf_options.max_visual_passes, 1);
}

TEST(CP1VisualPassConfig, ExactYamlIntegersOneAndTwoAreAcceptedAtStartup) {
  const std::vector<std::pair<int, std::string>> cases{{1, "nullspace"}, {2, "schur"}};
  for (const auto &test_case : cases) {
    SCOPED_TRACE(test_case.first);
    const TemporaryYaml yaml(full_msckf_yaml(std::to_string(test_case.first), test_case.second));
    const auto parser = parser_for(yaml);
    VioManagerOptions options;
    options.load_and_validate_msckf_update_configuration(parser);
    EXPECT_EQ(options.msckf_options.max_visual_passes, test_case.first);
    EXPECT_TRUE(parser->successful());
  }
}

TEST(CP1VisualPassConfig, NumericValuesOutsideOneAndTwoFailStartup) {
  const std::vector<int> invalid_counts{0, -1, 3, std::numeric_limits<int>::max()};
  for (const int count : invalid_counts) {
    SCOPED_TRACE(count);
    expect_yaml_startup_rejection(std::to_string(count), "must be exactly 1 or 2");
  }
  // OpenCV stores YAML integer scalars as signed int and wraps this lexical
  // value. The wrapped value must still be rejected by the closed 1/2 domain.
  expect_yaml_startup_rejection("2147483648", "must be exactly 1 or 2");
}

TEST(CP1VisualPassConfig, MalformedYamlScalarsNeverCoerceOrMutate) {
  const std::vector<std::pair<std::string, std::string>> malformed{
      {"quoted integer", "\"2\""},
      {"unknown spelling", "two"},
      {"real", "2.0"},
      {"boolean", "true"},
      {"sequence", "[ 2 ]"},
      {"map", "{ value: 2 }"},
  };

  for (const auto &test_case : malformed) {
    SCOPED_TRACE(test_case.first);
    const TemporaryYaml yaml("up_msckf_max_visual_passes: " + test_case.second + "\n");
    const auto parser = parser_for(yaml);
    int destination = 17;
    const YamlParser::OptionalExactIntResult parsed =
        parser->parse_optional_exact_int("up_msckf_max_visual_passes", destination);
    EXPECT_TRUE(parsed.status == YamlParser::OptionalExactIntStatus::WRONG_TYPE ||
                parsed.status == YamlParser::OptionalExactIntStatus::READ_ERROR);
    EXPECT_EQ(parsed.source, YamlParser::OptionalExactIntSource::YAML);
    EXPECT_EQ(destination, 17);

    expect_yaml_startup_rejection(test_case.second, "expected an exact integer scalar");
  }
}

#if ROS_AVAILABLE == 1
TEST(CP1VisualPassConfig, Ros1DecoderRequiresXmlRpcIntegerWithoutMutation) {
  int destination = 17;
  EXPECT_EQ(YamlParser::decode_ros_optional_exact_int(XmlRpc::XmlRpcValue(1), destination),
            YamlParser::OptionalExactIntStatus::ACCEPTED);
  EXPECT_EQ(destination, 1);
  EXPECT_EQ(YamlParser::decode_ros_optional_exact_int(XmlRpc::XmlRpcValue(2), destination),
            YamlParser::OptionalExactIntStatus::ACCEPTED);
  EXPECT_EQ(destination, 2);

  std::vector<XmlRpc::XmlRpcValue> malformed{
      XmlRpc::XmlRpcValue(true), XmlRpc::XmlRpcValue(2.0), XmlRpc::XmlRpcValue(std::string("2"))};
  XmlRpc::XmlRpcValue sequence;
  sequence.setSize(1);
  sequence[0] = 2;
  malformed.push_back(sequence);
  XmlRpc::XmlRpcValue map;
  map["value"] = 2;
  malformed.push_back(map);

  for (const auto &raw_value : malformed) {
    destination = 17;
    EXPECT_EQ(YamlParser::decode_ros_optional_exact_int(raw_value, destination),
              YamlParser::OptionalExactIntStatus::WRONG_TYPE);
    EXPECT_EQ(destination, 17);
  }
}
#elif ROS_AVAILABLE == 2
TEST(CP1VisualPassConfig, Ros2DecoderRequiresInRangeIntegerWithoutMutation) {
  int destination = 17;
  EXPECT_EQ(YamlParser::decode_ros_optional_exact_int(rclcpp::Parameter("passes", std::int64_t{2}), destination),
            YamlParser::OptionalExactIntStatus::ACCEPTED);
  EXPECT_EQ(destination, 2);

  const std::vector<rclcpp::Parameter> wrong_types{
      rclcpp::Parameter("passes", true), rclcpp::Parameter("passes", 2.0), rclcpp::Parameter("passes", std::string("2"))};
  for (const auto &raw_value : wrong_types) {
    destination = 17;
    EXPECT_EQ(YamlParser::decode_ros_optional_exact_int(raw_value, destination),
              YamlParser::OptionalExactIntStatus::WRONG_TYPE);
    EXPECT_EQ(destination, 17);
  }

  destination = 17;
  EXPECT_EQ(YamlParser::decode_ros_optional_exact_int(
                rclcpp::Parameter("passes", static_cast<std::int64_t>(std::numeric_limits<int>::max()) + 1), destination),
            YamlParser::OptionalExactIntStatus::READ_ERROR);
  EXPECT_EQ(destination, 17);
}
#endif

TEST(CP1VisualPassConfig, PassCountRemainsIndependentOfReducerSelection) {
  const TemporaryYaml yaml("up_msckf_max_visual_passes: 2\n");
  const auto parser = parser_for(yaml);
  VioManagerOptions options;
  options.msckf_options.landmark_elimination = UpdaterOptions::LandmarkElimination::NULLSPACE;
  options.load_msckf_max_visual_passes_or_exit(parser);

  EXPECT_EQ(options.msckf_options.max_visual_passes, 2);
  EXPECT_EQ(options.msckf_options.landmark_elimination, UpdaterOptions::LandmarkElimination::NULLSPACE);
}

TEST(CP1VisualPassConfig, SupportedReducerAndPassCombinationsValidate) {
  const std::vector<std::pair<int, UpdaterOptions::LandmarkElimination>> supported{
      {1, UpdaterOptions::LandmarkElimination::NULLSPACE},
      {1, UpdaterOptions::LandmarkElimination::SCHUR},
      {2, UpdaterOptions::LandmarkElimination::SCHUR},
  };
  for (const auto &test_case : supported) {
    SCOPED_TRACE(test_case.first);
    VioManagerOptions options;
    options.msckf_options.max_visual_passes = test_case.first;
    options.msckf_options.landmark_elimination = test_case.second;
    options.validate_msckf_update_configuration_or_exit();
    EXPECT_TRUE(UpdaterOptions::visual_pass_combination_is_supported(test_case.first, test_case.second));
  }
}

TEST(CP1VisualPassConfig, TwoPassNullspaceFailsStartupClearly) {
  const TemporaryYaml yaml(full_msckf_yaml("2", "nullspace"));
  EXPECT_EXIT(
      {
        (void)::dup2(STDERR_FILENO, STDOUT_FILENO);
        const auto parser = std::make_shared<YamlParser>(yaml.path());
        VioManagerOptions options;
        options.load_and_validate_msckf_update_configuration(parser);
        std::exit(EXIT_SUCCESS);
      },
      ::testing::ExitedWithCode(EXIT_FAILURE),
      "up_msckf_max_visual_passes=2 requires up_msckf_landmark_elimination=schur");
}

} // namespace
