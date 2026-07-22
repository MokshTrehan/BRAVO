#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
repo_root="$(cd -- "${script_dir}/../.." && pwd -P)"
readonly ros_setup="/opt/ros/noetic/setup.bash"
readonly workspace="${repo_root}/build/cp0-ws"
readonly source_space="${workspace}/src"
readonly ceres_prefix="${repo_root}/build/vendor/ceres-install"
readonly ceres_dir="${ceres_prefix}/lib/cmake/Ceres"

jobs="${CP0_BUILD_JOBS:-$(getconf _NPROCESSORS_ONLN)}"
if [[ ! "${jobs}" =~ ^[1-9][0-9]*$ ]]; then
    echo "CP0_BUILD_JOBS must be a positive integer (got: ${jobs})" >&2
    exit 2
fi
if [[ ! -r "${ros_setup}" ]]; then
    echo "ROS Noetic setup file is missing: ${ros_setup}" >&2
    exit 2
fi
if [[ ! -x /usr/bin/python3 ]]; then
    echo "the ROS build requires /usr/bin/python3" >&2
    exit 2
fi

"${script_dir}/bootstrap_ceres_1_14.sh"

# shellcheck disable=SC1091
source "${ros_setup}"
if ! command -v catkin >/dev/null 2>&1; then
    echo "catkin_tools is required (expected the 'catkin' command)" >&2
    exit 2
fi

mkdir -p -- "${source_space}"
for package in ov_core ov_data ov_eval ov_init ov_msckf config; do
    source_path="${repo_root}/${package}"
    link_path="${source_space}/${package}"
    if [[ ! -e "${source_path}" ]]; then
        echo "source path is missing: ${source_path}" >&2
        exit 2
    fi
    if [[ -L "${link_path}" ]]; then
        if [[ "$(readlink -f -- "${link_path}")" != "$(readlink -f -- "${source_path}")" ]]; then
            echo "workspace link points elsewhere; refusing to replace: ${link_path}" >&2
            exit 2
        fi
    elif [[ -e "${link_path}" ]]; then
        echo "workspace entry is not the expected symlink: ${link_path}" >&2
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
        -DDISABLE_MATPLOTLIB=ON \
        -DPYTHON_EXECUTABLE=/usr/bin/python3 \
        -DPython_EXECUTABLE=/usr/bin/python3 \
        "-DCeres_DIR=${ceres_dir}" \
        "-DCMAKE_BUILD_RPATH=${ceres_prefix}/lib" \
        "-DCMAKE_INSTALL_RPATH=${ceres_prefix}/lib"

catkin build --workspace "${workspace}" --jobs "${jobs}" --no-status --summarize

# shellcheck disable=SC1091
source "${workspace}/devel/setup.bash"
executable="${workspace}/devel/lib/ov_msckf/ros1_serial_msckf"
for artifact in "${executable}"; do
    if [[ ! -x "${artifact}" ]]; then
        echo "expected ROS executable was not built: ${artifact}" >&2
        exit 1
    fi
done
if ! ldd "${executable}" | grep -F "${ceres_prefix}/lib/libceres.so.1" >/dev/null; then
    echo "ros1_serial_msckf is not linked to the pinned local Ceres build" >&2
    ldd "${executable}" | grep -E 'ceres|not found' >&2 || true
    exit 1
fi
if [[ "$(rospack find ov_msckf)" != "${source_space}/ov_msckf" ]]; then
    echo "ROS package resolution does not point at the CP0 workspace" >&2
    exit 1
fi

/usr/bin/python3 "${script_dir}/build_provenance.py" \
    --write "${workspace}/CP0_BUILD_PROVENANCE.json" \
    --executable "${executable}" \
    --ceres-library "${ceres_prefix}/lib/libceres.so.1.14.0" \
    --artifact "${workspace}/devel/lib/libov_core_lib.so" \
    --artifact "${workspace}/devel/lib/libov_init_lib.so" \
    --artifact "${workspace}/devel/lib/libov_msckf_lib.so"

echo "ROS1 CP0 workspace built successfully with /usr/bin/python3:"
echo "  ${workspace}"
