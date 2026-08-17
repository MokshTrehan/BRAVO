#!/usr/bin/env bash
# C8 yield rule (DESKTOP_EVIDENCE_SESSION v2): never make EXT-VF-1 wait.
# Blocks (polling every 300 s) while any VINS-Fusion estimator/container process is present.
# Usage: c8_yield_check.sh <RUN_LOG path> [label]
LOG="$1"; LABEL="${2:-batch}"
pat='vins_estimator|vins_node|loop_fusion|vins-fusion|vins_fusion|rosbag play|melodic'
while :; do
  hits=$( (ps -eo pid,etimes,cmd | grep -iE "$pat" | grep -vE 'grep|c8_yield_check') ; (docker ps --format '{{.ID}} {{.Image}} {{.Names}}' 2>/dev/null) )
  if [ -z "$(echo "$hits" | tr -d '[:space:]')" ]; then
    echo "- $(date -u +%FT%TZ) yield-check [$LABEL]: no EXT-VF-1 processes/containers -> proceed" >> "$LOG"; exit 0
  fi
  echo "- $(date -u +%FT%TZ) yield-check [$LABEL]: EXT-VF-1 activity present, waiting 300 s: $(echo "$hits" | head -3 | tr '\n' ';' | cut -c1-300)" >> "$LOG"
  sleep 300
done
