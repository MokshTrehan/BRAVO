/*
 * Standalone offline runner for schema-1 camera-conditioning systems.
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * The Python capture validator exports the small, checksummed capture into the
 * deterministic CCSYSTEMS1 interchange consumed here. Numerical results and
 * nondeterministic wall-clock samples are deliberately written separately.
 */

#include "CameraConditioningComparators.h"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>
#include <vector>

namespace {

using ov_msckf::conditioning::CameraSystem;
using ov_msckf::conditioning::Comparison;
using ov_msckf::conditioning::Method;
using ov_msckf::conditioning::MethodResult;

constexpr char kInterchangeMagic[] = "SCVIOCCSYSTEMS1";
constexpr std::uint64_t kInterchangeSchema = 1;
constexpr int kTimingRepetitions = 9;
volatile double g_timing_sink = 0.0;
constexpr const char *kNullspaceSourceSha256 =
    "f68fc2e1dd04a6a33e94c576839dc4643114d2100ddb69f1b00da09bd7b12714";
constexpr const char *kNullspaceGitBlob =
    "b36c004f78093b48d483bccef38726eb4f7c7810";
constexpr const char *kSchurSourceSha256 =
    "2237619f8dd81054a9722967f356766182980e4e20f86a4145002aff7aa66c03";

struct Record {
  std::string sequence;
  std::string capture_sha256;
  std::string trailer_sha256;
  std::string source_commit;
  std::string config_sha256;
  std::uint64_t record_index = 0;
  std::uint64_t update_index = 0;
  std::uint64_t feature_ordinal = 0;
  std::uint64_t feature_id = 0;
  std::uint64_t observation_count = 0;
  std::uint64_t camera_model = 0;
  bool fej_enabled = false;
  bool geometry_valid = false;
  std::uint64_t calibration_flags = 0;
  double timestamp = 0.0;
  double minimum_depth = 0.0;
  double maximum_parallax_rad = 0.0;
  int recorded_rank = 0;
  bool recorded_ratio_available = false;
  double recorded_singular_ratio = 0.0;
  CameraSystem system;
};

void read_exact(std::istream &stream, char *destination, std::size_t size,
                const char *what) {
  stream.read(destination, static_cast<std::streamsize>(size));
  if (stream.gcount() != static_cast<std::streamsize>(size)) {
    throw std::runtime_error(std::string("truncated ") + what);
  }
}

std::uint64_t read_u64(std::istream &stream, const char *what) {
  unsigned char bytes[8];
  read_exact(stream, reinterpret_cast<char *>(bytes), sizeof(bytes), what);
  std::uint64_t value = 0;
  for (const unsigned char byte : bytes) {
    value = (value << 8U) | static_cast<std::uint64_t>(byte);
  }
  return value;
}

std::int64_t read_i64(std::istream &stream, const char *what) {
  const std::uint64_t bits = read_u64(stream, what);
  std::int64_t value = 0;
  std::memcpy(&value, &bits, sizeof(value));
  return value;
}

double read_f64(std::istream &stream, const char *what) {
  const std::uint64_t bits = read_u64(stream, what);
  double value = 0.0;
  std::memcpy(&value, &bits, sizeof(value));
  return value;
}

std::string read_string(std::istream &stream, const char *what) {
  const std::uint64_t size = read_u64(stream, what);
  if (size > (1U << 20U)) {
    throw std::runtime_error(std::string(what) + " exceeds string limit");
  }
  std::string value(static_cast<std::size_t>(size), '\0');
  if (size != 0) {
    read_exact(stream, &value[0], static_cast<std::size_t>(size), what);
  }
  return value;
}

Eigen::MatrixXd read_matrix(std::istream &stream, const char *what) {
  const std::uint64_t rows = read_u64(stream, what);
  const std::uint64_t cols = read_u64(stream, what);
  if (rows > (1U << 20U) || cols > (1U << 20U) ||
      (cols != 0 && rows > (4U << 20U) / cols)) {
    throw std::runtime_error(std::string(what) + " exceeds matrix limit");
  }
  Eigen::MatrixXd matrix(static_cast<Eigen::Index>(rows),
                         static_cast<Eigen::Index>(cols));
  for (Eigen::Index row = 0; row < matrix.rows(); ++row) {
    for (Eigen::Index column = 0; column < matrix.cols(); ++column) {
      matrix(row, column) = read_f64(stream, what);
    }
  }
  return matrix;
}

std::vector<Record> read_records(const std::string &path) {
  std::ifstream stream(path, std::ios::binary);
  if (!stream) {
    throw std::runtime_error("cannot open interchange input " + path);
  }
  char magic[sizeof(kInterchangeMagic) - 1];
  read_exact(stream, magic, sizeof(magic), "interchange magic");
  if (std::memcmp(magic, kInterchangeMagic, sizeof(magic)) != 0) {
    throw std::runtime_error("interchange magic mismatch");
  }
  if (read_u64(stream, "interchange schema") != kInterchangeSchema) {
    throw std::runtime_error("interchange schema mismatch");
  }
  const std::uint64_t count = read_u64(stream, "record count");
  if (count > (1U << 30U)) {
    throw std::runtime_error("record count exceeds limit");
  }
  std::vector<Record> records;
  records.reserve(static_cast<std::size_t>(count));
  for (std::uint64_t index = 0; index < count; ++index) {
    Record record;
    record.sequence = read_string(stream, "sequence");
    record.capture_sha256 = read_string(stream, "capture SHA-256");
    record.trailer_sha256 = read_string(stream, "trailer SHA-256");
    record.source_commit = read_string(stream, "source commit");
    record.config_sha256 = read_string(stream, "config SHA-256");
    record.record_index = read_u64(stream, "record index");
    record.update_index = read_u64(stream, "update index");
    record.feature_ordinal = read_u64(stream, "feature ordinal");
    record.feature_id = read_u64(stream, "feature id");
    record.observation_count = read_u64(stream, "observation count");
    record.camera_model = read_u64(stream, "camera model");
    const std::uint64_t fej = read_u64(stream, "FEJ flag");
    if (fej > 1) {
      throw std::runtime_error("FEJ flag is not boolean");
    }
    record.fej_enabled = fej != 0;
    const std::uint64_t geometry_valid = read_u64(stream, "geometry-valid flag");
    if (geometry_valid > 1) {
      throw std::runtime_error("geometry-valid flag is not boolean");
    }
    record.geometry_valid = geometry_valid != 0;
    record.calibration_flags = read_u64(stream, "calibration flags");
    record.timestamp = read_f64(stream, "timestamp");
    record.minimum_depth = read_f64(stream, "minimum depth");
    record.maximum_parallax_rad = read_f64(stream, "maximum parallax");
    record.recorded_rank = static_cast<int>(read_i64(stream, "recorded rank"));
    const std::uint64_t ratio_available =
        read_u64(stream, "recorded ratio available");
    if (ratio_available > 1) {
      throw std::runtime_error("recorded ratio flag is not boolean");
    }
    record.recorded_ratio_available = ratio_available != 0;
    record.recorded_singular_ratio = read_f64(stream, "recorded singular ratio");
    record.system.sigma_px = read_f64(stream, "sigma_px");
    record.system.H_x = read_matrix(stream, "H_x");
    record.system.H_f = read_matrix(stream, "H_f");
    const Eigen::MatrixXd residual = read_matrix(stream, "residual");
    if (residual.cols() != 1) {
      throw std::runtime_error("residual interchange matrix is not a column");
    }
    record.system.residual = residual.col(0);
    record.system.P_active = read_matrix(stream, "P_active");
    records.push_back(std::move(record));
  }
  if (stream.get() != std::char_traits<char>::eof()) {
    throw std::runtime_error("trailing bytes in interchange input");
  }
  return records;
}

std::string csv_escape(const std::string &value) {
  if (value.find_first_of(",\"\r\n") == std::string::npos) {
    return value;
  }
  std::string escaped = "\"";
  for (const char character : value) {
    escaped += character == '\"' ? "\"\"" : std::string(1, character);
  }
  escaped += '\"';
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

void write_prefix(std::ostream &stream, const Record &record,
                  const Comparison &comparison) {
  stream << csv_escape(record.sequence) << ',' << csv_escape(record.capture_sha256)
         << ',' << csv_escape(record.trailer_sha256) << ','
         << csv_escape(record.source_commit) << ','
         << csv_escape(record.config_sha256) << ',' << record.record_index << ','
         << record.update_index << ',' << record.feature_ordinal << ','
         << record.feature_id << ',';
  write_double(stream, record.timestamp);
  stream << ',' << record.observation_count << ',' << record.observation_count << ','
         << record.camera_model << ',' << static_cast<int>(record.fej_enabled) << ','
         << static_cast<int>(record.geometry_valid) << ','
         << record.calibration_flags << ',';
  write_double(stream, record.minimum_depth);
  stream << ',';
  write_double(stream, record.maximum_parallax_rad);
  stream << ',' << record.recorded_rank << ','
         << static_cast<int>(record.recorded_ratio_available) << ',';
  write_double(stream, record.recorded_singular_ratio);
  stream << ',' << comparison.rank.numerical_rank << ','
         << static_cast<int>(comparison.rank.singular_ratio_available) << ',';
  write_double(stream, comparison.rank.singular_ratio);
  stream << ',' << static_cast<int>(comparison.rank.frozen_guard_accepts) << ','
         << csv_escape(comparison.rank.guard_status) << ','
         << csv_escape(comparison.rank.guard_stage) << ','
         << static_cast<int>(comparison.oracle_agreement.oracle_safe_opportunity)
         << ',';
  write_double(stream, comparison.oracle_agreement.state_increment_relative_error);
  stream << ',';
  write_double(stream,
               comparison.oracle_agreement.posterior_covariance_relative_error);
  stream << ',' << static_cast<int>(comparison.unguarded_nullspace_harmful) << ','
         << static_cast<int>(comparison.caller_input_mutated) << ',';
}

void write_result_header(std::ostream &stream) {
  stream
      << "sequence,capture_sha256,trailer_sha256,source_commit,config_sha256,"
         "record_index,update_index,feature_ordinal,feature_id,timestamp,observation_count,track_length,"
         "camera_model,fej_enabled,geometry_valid,calibration_flags,minimum_depth,maximum_parallax_rad,"
         "recorded_rank,recorded_ratio_available,recorded_singular_ratio,numerical_rank,"
         "singular_ratio_available,singular_ratio,frozen_guard_accepts,guard_status,"
         "guard_stage,oracle_safe_opportunity,oracle_state_increment_relative_error,"
         "oracle_posterior_covariance_relative_error,unguarded_nullspace_harmful,"
         "caller_input_mutated,method,implementation_label,source_identity,accepted,status,reason,finite,harmful_accepted,"
         "input_mutation,mutation_expected,unexplained_input_mutation,"
         "state_increment_relative_error,posterior_covariance_relative_error,"
         "nis_relative_error,scaled_psd_failure,minimum_covariance_eigenvalue,"
         "psd_tolerance,supported_subspace_trace,log_pseudodeterminant,effective_rank,"
         "half_logdet_identity_plus_information,correction_norm,nis,gamma\n";
}

const char *implementation_label(Method method) {
  switch (method) {
  case Method::kUpstreamNullspace:
    return "exact_upstream_source_identical_alias";
  case Method::kLocalNullspace:
    return "actual_local_production_nullspace";
  case Method::kGuardedNullspaceDrop:
    return "frozen_svd_guard_then_actual_production_nullspace";
  case Method::kRankAwareNullspace:
    return "offline_full_u_rank_aware_nullspace";
  case Method::kLocalSchur:
    return "actual_local_SchurUpdate_Reduce";
  case Method::kFullJointOracle:
    return "offline_psd_support_full_joint_svd";
  case Method::kFullUNullspaceOracle:
    return "offline_psd_support_full_u_svd";
  }
  return "unknown";
}

std::string source_identity(Method method) {
  switch (method) {
  case Method::kUpstreamNullspace:
    return std::string(kNullspaceSourceSha256) + ":blob=" + kNullspaceGitBlob;
  case Method::kLocalNullspace:
  case Method::kGuardedNullspaceDrop:
    return kNullspaceSourceSha256;
  case Method::kLocalSchur:
    return kSchurSourceSha256;
  case Method::kRankAwareNullspace:
  case Method::kFullJointOracle:
  case Method::kFullUNullspaceOracle:
    return "camera_conditioning_comparator_source";
  }
  return "unknown";
}

void write_result(std::ostream &stream, const Record &record,
                  const Comparison &comparison, const MethodResult &result) {
  write_prefix(stream, record, comparison);
  stream << csv_escape(ov_msckf::conditioning::method_name(result.method)) << ','
         << csv_escape(implementation_label(result.method)) << ','
         << csv_escape(source_identity(result.method)) << ','
         << static_cast<int>(result.accepted) << ',' << csv_escape(result.status)
         << ',' << csv_escape(result.reason) << ',' << static_cast<int>(result.finite)
         << ',' << static_cast<int>(result.harmful_accepted) << ','
         << static_cast<int>(result.input_mutation) << ','
         << static_cast<int>(result.mutation_expected) << ','
         << static_cast<int>(result.unexplained_input_mutation) << ',';
  write_double(stream, result.state_increment_relative_error);
  stream << ',';
  write_double(stream, result.posterior_covariance_relative_error);
  stream << ',';
  write_double(stream, result.nis_relative_error);
  stream << ',' << static_cast<int>(result.scaled_psd_failure) << ',';
  write_double(stream, result.posterior.minimum_covariance_eigenvalue);
  stream << ',';
  write_double(stream, result.posterior.psd_tolerance);
  stream << ',';
  write_double(stream, result.information.supported_subspace_trace);
  stream << ',';
  write_double(stream, result.information.log_pseudodeterminant);
  stream << ',' << result.information.effective_rank << ',';
  write_double(stream,
               result.information.half_logdet_identity_plus_information);
  stream << ',';
  write_double(stream, result.information.correction_norm);
  stream << ',';
  write_double(stream, result.posterior.nis);
  stream << ',';
  write_double(stream, result.gamma);
  stream << '\n';
}

double median(std::vector<double> values) {
  std::sort(values.begin(), values.end());
  return values[values.size() / 2];
}

void write_timing_header(std::ostream &stream) {
  stream << "sequence,capture_sha256,record_index,method,repetitions,"
            "offline_end_to_end_median_runtime_ns\n";
}

void write_timing(std::ostream &stream, const Record &record, Method method) {
  std::vector<double> samples;
  samples.reserve(kTimingRepetitions);
  for (int repetition = 0; repetition < kTimingRepetitions; ++repetition) {
    const auto start = std::chrono::steady_clock::now();
    const MethodResult result =
        ov_msckf::conditioning::evaluate_method(record.system, method, nullptr);
    const auto stop = std::chrono::steady_clock::now();
    samples.push_back(
        std::chrono::duration<double, std::nano>(stop - start).count());
    g_timing_sink += result.accepted && result.posterior.dx.size() > 0
                         ? result.posterior.dx(0)
                         : 0.0;
  }
  stream << csv_escape(record.sequence) << ',' << csv_escape(record.capture_sha256)
         << ',' << record.record_index << ','
         << csv_escape(ov_msckf::conditioning::method_name(method)) << ','
         << kTimingRepetitions << ',';
  write_double(stream, median(samples));
  stream << '\n';
}

} // namespace

int main(int argc, char **argv) {
  if (argc != 4) {
    std::cerr << "usage: " << argv[0]
              << " INPUT.ccsystems REAL_CONDITIONING_RESULTS.csv TIMING.csv\n";
    return 64;
  }
  try {
    const std::vector<Record> records = read_records(argv[1]);
    std::ofstream results(argv[2]);
    std::ofstream timing(argv[3]);
    if (!results || !timing) {
      throw std::runtime_error("cannot open comparator output");
    }
    write_result_header(results);
    write_timing_header(timing);
    const Method timing_methods[] = {
        Method::kUpstreamNullspace,
        Method::kLocalNullspace,
        Method::kGuardedNullspaceDrop,
        Method::kRankAwareNullspace,
        Method::kLocalSchur,
        Method::kFullJointOracle,
        Method::kFullUNullspaceOracle,
    };
    for (std::size_t index = 0; index < records.size(); ++index) {
      const Comparison comparison =
          ov_msckf::conditioning::evaluate_all(records[index].system);
      for (const MethodResult &result : comparison.methods) {
        write_result(results, records[index], comparison, result);
      }
      for (const Method method : timing_methods) {
        write_timing(timing, records[index], method);
      }
      if ((index + 1U) % 1000U == 0U) {
        std::cerr << "compared systems=" << index + 1U << '\n';
      }
    }
    std::cerr << "timing_sink=" << std::setprecision(17) << g_timing_sink << '\n';
    std::cout << "camera conditioning comparison complete systems=" << records.size()
              << " method_rows=" << records.size() * 7U << '\n';
    return 0;
  } catch (const std::exception &error) {
    std::cerr << "camera conditioning comparison failed: " << error.what() << '\n';
    return 1;
  }
}
