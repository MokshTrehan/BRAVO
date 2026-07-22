#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
repo_root="$(cd -- "${script_dir}/../.." && pwd -P)"
readonly ros_setup="/opt/ros/noetic/setup.bash"
readonly workspace_setup="${repo_root}/build/cp0-ws/devel/setup.bash"

if [[ ! -r "${workspace_setup}" ]]; then
    echo "CP0 workspace is not built; run scripts/cp0/build_ros1.sh first" >&2
    exit 2
fi

# Catkin setup scripts inspect positional parameters. Do not let CP0 CLI flags
# (notably --help) leak into their generated setup utility.
saved_arguments=("$@")
set --
# shellcheck disable=SC1091
source "${ros_setup}"
# shellcheck disable=SC1091
source "${workspace_setup}"
set -- "${saved_arguments[@]}"
exec /usr/bin/python3 "${script_dir}/run_mh01.py" "$@"
