#!/usr/bin/env bash
# 1 Hz host sampler: epoch, thermal zone temps (mC), cooling device cur_state, cpu1 freq. Runs until killed.
out=$1
zones=(0 1 5 6 7 8); devs=(0 1 2 3 4 8 9 10 11 12)
{
  printf '#epoch'
  for z in "${zones[@]}"; do printf ' temp:%s' "$(cat /sys/class/thermal/thermal_zone$z/type)"; done
  for d in "${devs[@]}"; do printf ' cd:%s' "$(cat /sys/class/thermal/cooling_device$d/type)"; done
  printf ' cpu1_khz\n'
} > "$out"
while true; do
  line=$(date +%s.%N)
  for z in "${zones[@]}"; do line="$line $(cat /sys/class/thermal/thermal_zone$z/temp 2>/dev/null || echo NA)"; done
  for d in "${devs[@]}"; do line="$line $(cat /sys/class/thermal/cooling_device$d/cur_state 2>/dev/null || echo NA)"; done
  line="$line $(cat /sys/devices/system/cpu/cpu1/cpufreq/scaling_cur_freq 2>/dev/null || echo NA)"
  echo "$line" >> "$out"
  sleep 1
done
