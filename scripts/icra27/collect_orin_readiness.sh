#!/usr/bin/env bash
# Read-only Jetson Orin Nano Developer Kit readiness inventory.

set -u

export LANG=C
export LC_ALL=C
export TZ=UTC

section() {
  printf '\n[%s]\n' "$1"
}

run() {
  printf '$'
  printf ' %q' "$@"
  printf '\n'
  "$@" 2>&1
  local status=$?
  if [[ ${status} -ne 0 ]]; then
    printf 'command_status=%d\n' "${status}"
  fi
  return 0
}

read_nul_file() {
  local path=$1
  if [[ -r ${path} ]]; then
    tr '\0' '\n' < "${path}"
  else
    printf 'UNAVAILABLE: %s\n' "${path}"
  fi
}

model_path=/proc/device-tree/model
if [[ -r ${model_path} ]]; then
  model=$(tr -d '\0' < "${model_path}")
else
  model=
fi
if [[ ${model} != *Jetson*Orin*Nano* ]]; then
  printf 'REFUSED: this host does not identify as a Jetson Orin Nano: %s\n' \
    "${model:-unknown}" >&2
  exit 2
fi

section identity
printf 'collector_schema=schurvio.icra27.orin_readiness.v1\n'
printf 'captured_utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
printf 'device_model=%s\n' "${model}"
run uname -a
run cat /etc/os-release
run cat /etc/nv_tegra_release
section device_tree_compatible
read_nul_file /proc/device-tree/compatible

section software
run dpkg-query -W -f='${Package}\t${Version}\n' \
  nvidia-jetpack nvidia-l4t-core nvidia-l4t-kernel nvidia-l4t-tools
run gcc --version
run g++ --version
run cmake --version
run python3 --version
run bash -c 'command -v rosversion >/dev/null && rosversion -d || printf "ROS_NOT_FOUND\n"'
run bash -c 'command -v catkin >/dev/null && catkin --version || printf "CATKIN_NOT_FOUND\n"'

section compute_and_storage
run lscpu
run getconf _NPROCESSORS_ONLN
run cat /sys/devices/system/cpu/online
run free -b
run lsblk -b -o NAME,TYPE,SIZE,FSTYPE,MOUNTPOINTS,MODEL
run df -B1 /
run bash -c 'for path in /sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_governor; do [[ -r "$path" ]] && printf "%s=" "$path" && cat "$path"; done'

section network_and_time
run hostname
run hostname -I
run ip -brief address
run timedatectl show -p Timezone -p NTPSynchronized -p LocalRTC

section power_profile_and_clocks
run sudo -n /usr/sbin/nvpmodel -q --verbose
run sudo -n /usr/bin/jetson_clocks --show
run bash -c 'command -v nvfancontrol >/dev/null && systemctl status nvfancontrol --no-pager || printf "NVFANCONTROL_NOT_FOUND\n"'

section telemetry_sample
run timeout --signal=INT 6s sudo -n /usr/bin/tegrastats --interval 1000

section power_and_thermal_interfaces
run bash -c 'find /sys -type f \( -name "in_power*_input" -o -name "in_curr*_input" -o -name "in_voltage*_input" -o -name "oc*_event_cnt" \) -readable -print 2>/dev/null | sort'
run bash -c 'for zone in /sys/class/thermal/thermal_zone*; do [[ -r "$zone/type" && -r "$zone/temp" ]] && printf "%s\t" "$zone" && cat "$zone/type" "$zone/temp" | tr "\n" "\t" && printf "\n"; done'

section ssh_service
run systemctl is-enabled ssh
run systemctl is-active ssh

printf '\nCOLLECTION_COMPLETE\n'
