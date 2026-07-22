#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
set -Eeuo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
repo_root="$(cd -- "${script_dir}/../.." && pwd -P)"
readonly ros_setup="/opt/ros/noetic/setup.bash"
readonly workspace="${repo_root}/build/cp1-ws"
readonly source_space="${workspace}/src"
readonly ceres_prefix="${repo_root}/build/vendor/ceres-install"
readonly ceres_dir="${ceres_prefix}/lib/cmake/Ceres"

jobs="${CP1_BUILD_JOBS:-$(getconf _NPROCESSORS_ONLN)}"
if [[ ! "${jobs}" =~ ^[1-9][0-9]*$ ]]; then
    echo "CP1_BUILD_JOBS must be a positive integer (got: ${jobs})" >&2
    exit 2
fi
if [[ ! -r "${ros_setup}" ]]; then
    echo "ROS Noetic setup file is missing: ${ros_setup}" >&2
    exit 2
fi

"${repo_root}/scripts/cp0/bootstrap_ceres_1_14.sh"

# shellcheck disable=SC1091
source "${ros_setup}"
mkdir -p -- "${source_space}"
for package in ov_core ov_data ov_eval ov_init ov_msckf config; do
    source_path="${repo_root}/${package}"
    link_path="${source_space}/${package}"
    if [[ -L "${link_path}" ]]; then
        if [[ "$(readlink -f -- "${link_path}")" != "$(readlink -f -- "${source_path}")" ]]; then
            echo "CP1 workspace link points elsewhere: ${link_path}" >&2
            exit 2
        fi
    elif [[ -e "${link_path}" ]]; then
        echo "CP1 workspace entry is not the expected symlink: ${link_path}" >&2
        exit 2
    else
        ln -s -- "${source_path}" "${link_path}"
    fi
done

catkin config --workspace "${workspace}" \
    --extend /opt/ros/noetic \
    --merge-devel \
    --cmake-args \
        -DCMAKE_BUILD_TYPE=RelWithDebInfo \
        -DCATKIN_ENABLE_TESTING=ON \
        -DBUILD_TESTING=ON \
        -DDISABLE_MATPLOTLIB=ON \
        -DPYTHON_EXECUTABLE=/usr/bin/python3 \
        -DPython_EXECUTABLE=/usr/bin/python3 \
        "-DCeres_DIR=${ceres_dir}" \
        "-DCMAKE_BUILD_RPATH=${ceres_prefix}/lib" \
        "-DCMAKE_INSTALL_RPATH=${ceres_prefix}/lib"

catkin build --workspace "${workspace}" ov_msckf --jobs "${jobs}" --no-status --summarize

# catkin_add_gtest deliberately marks test executables EXCLUDE_FROM_ALL.  Build
# the three CP1 gate targets explicitly before looking for or running them.
cmake --build "${workspace}/build/ov_msckf" \
    --target \
        test_cp1_schur_equivalence \
        test_cp1_rank_rejection \
        test_cp1_projection_jacobian \
    -- -j"${jobs}"

run_id="cp1_math_$(date -u +%Y%m%dT%H%M%S%NZ)-g$(git -C "${repo_root}" rev-parse --short=12 HEAD)"
artifact_dir="${repo_root}/results/staging/cp1/${run_id}"
mkdir -p -- "${artifact_dir}"

test_names=(
    test_cp1_schur_equivalence
    test_cp1_rank_rejection
    test_cp1_projection_jacobian
)
binary_args=()
for test_name in "${test_names[@]}"; do
    binary="${workspace}/devel/lib/ov_msckf/${test_name}"
    if [[ ! -x "${binary}" ]]; then
        echo "expected CP1 test binary is missing: ${binary}" >&2
        exit 1
    fi
    binary_args+=(--binary "${binary}")
    env -u GTEST_FILTER -u GTEST_TOTAL_SHARDS -u GTEST_SHARD_INDEX -u GTEST_OUTPUT \
        OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 \
        "${binary}" \
        --gtest_color=no \
        --gtest_filter='*' \
        --gtest_repeat=1 \
        --gtest_shuffle=0 \
        "--gtest_output=xml:${artifact_dir}/${test_name}.xml" \
        2>&1 | tee "${artifact_dir}/${test_name}.log"
done

/usr/bin/python3 "${script_dir}/make_report.py" \
    "${artifact_dir}" "${repo_root}" "${binary_args[@]}"
/usr/bin/python3 "${script_dir}/verify_report.py" "${artifact_dir}" "${repo_root}"

echo "CP1 automated evidence retained under staging pending human signoff:"
echo "  ${artifact_dir}"
