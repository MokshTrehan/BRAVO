#!/usr/bin/env bash
# C5 Orin bring-up cell runner — executes INSIDE the schurvio-orin-env:c5
# container. One invocation = one estimator replay cell in the established
# layout (trajectory/ diagnostics/ ros-home/ ros-logs/ console.log SHA256SUMS).
#
# Usage: orin_cell_run.sh <cell_dir> <ros_port> <cpu_list> <launch_file> \
#          <bag> <bag_start> <dataset:kaist|euroc> [extra roslaunch args...]
set -Eeuo pipefail

cell_dir=$1; ros_port=$2; cpu_list=$3; launch_file=$4
bag=$5; bag_start=$6; dataset=$7; shift 7

mkdir -p "${cell_dir}"/{trajectory,diagnostics,ros-home,ros-logs,isolated-home}
export HOME="${cell_dir}/isolated-home"
export ROS_HOME="${cell_dir}/ros-home"
export ROS_LOG_DIR="${cell_dir}/ros-logs"
export ROS_MASTER_URI="http://localhost:${ros_port}"
export LANG=C.UTF-8 LC_ALL=C.UTF-8
export CUDA_VISIBLE_DEVICES=""

source /opt/ros/noetic/setup.bash
source /repo/build/cp0-ws/devel/setup.bash

roscore -p "${ros_port}" > "${cell_dir}/diagnostics/roscore.log" 2>&1 &
roscore_pid=$!
for _ in $(seq 1 60); do
    if rostopic list > /dev/null 2>&1; then break; fi
    sleep 1
done
rostopic list > /dev/null 2>&1 || { echo "roscore failed to start" >&2; exit 3; }

path_state="${cell_dir}/trajectory/state_estimate.txt"
path_std="${cell_dir}/trajectory/state_deviation.txt"
path_time="${cell_dir}/diagnostics/timing_openvins.csv"

if [[ "${dataset}" == "kaist" ]]; then
    launch_args=(
        "bag:=${bag}"
        "candidate_config:=/repo/config/kaist_vio_rotation_robustness/estimator_config.yaml"
        "bag_start:=${bag_start}"
        "path_state:=${path_state}"
        "path_std:=${path_std}"
        "path_time:=${path_time}"
        "verbosity:=INFO"
    )
else
    launch_args=(
        "config_path:=/repo/config/euroc_mav/estimator_config.yaml"
        "bag:=${bag}"
        "bag_start:=${bag_start}"
        "bag_durr:=-1"
        "path_state:=${path_state}"
        "path_std:=${path_std}"
        "path_time:=${path_time}"
        "record_timing:=true"
    )
fi

status=0
/usr/bin/time --verbose \
    --output="${cell_dir}/diagnostics/resource_usage.txt" -- \
    taskset --cpu-list "${cpu_list}" \
    roslaunch --wait "${launch_file}" "${launch_args[@]}" "$@" \
    > "${cell_dir}/console.log" 2>&1 || status=$?

rosparam dump "${cell_dir}/diagnostics/resolved_ros_parameters.yaml" 2>/dev/null || true
kill -INT "${roscore_pid}" 2>/dev/null || true
wait "${roscore_pid}" 2>/dev/null || true

printf 'launch_file=%s\nbag=%s\nbag_start=%s\ncpu_list=%s\nros_port=%s\nroslaunch_exit=%s\nextra_args=%s\n' \
    "${launch_file}" "${bag}" "${bag_start}" "${cpu_list}" "${ros_port}" "${status}" "$*" \
    > "${cell_dir}/diagnostics/invocation.txt"

( cd "${cell_dir}" && find . -type f ! -name SHA256SUMS -print0 | sort -z \
    | xargs -0 sha256sum > SHA256SUMS )

echo "CELL_DONE exit=${status} cell=${cell_dir}"
exit "${status}"
