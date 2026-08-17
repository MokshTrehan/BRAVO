#!/usr/bin/env bash
# C8 Item 1 campaign launcher: capture (inertness) -> cycle (classes 1-6,8) -> class 7a (console) -> class 7b (T0 stream faults)
set -uo pipefail
ROOT="$1"; RUNLOG="$ROOT/RUN_LOG.md"
REPO=/home/moksh/schurvio-lite-rotation-robustness
cd "$REPO"
source build/cp0-ws/devel/setup.bash
DRV="python3 scripts/icra27/c8_fault_driver.py --root $ROOT --run-log $RUNLOG"
echo "- $(date -u +%FT%TZ) item1 campaign launcher start (shim sha256 $(sha256sum tools/c8_faultinj/libc8_faultinj_shim.so | cut -d' ' -f1))" >> "$RUNLOG"
$DRV --kind capture --tag capture
$DRV --kind cycle --stride 4 --tag cycle4
$DRV --kind capture --console-devfull --tag class7a-console
# class 7b: TurnSafe T0 diagnostics-stream faults; every stage x kind on circle.bag (order 22), kind 2 on MH_05 (order 5)
for kind in 2 1 3; do
  for stage in $(seq 1 29); do
    at=50; if [ $stage -eq 19 ] || [ $stage -eq 21 ] || [ $stage -eq 22 ]; then at=0; fi
    $DRV --kind t0 --orders 22 --t0-stage $stage --t0-kind $kind --t0-at $at --tag class7b-t0-s${stage}-k${kind}
  done
done
for stage in $(seq 1 29); do
  at=50; if [ $stage -eq 19 ] || [ $stage -eq 21 ] || [ $stage -eq 22 ]; then at=0; fi
  $DRV --kind t0 --orders 5 --t0-stage $stage --t0-kind 2 --t0-at $at --tag class7b-t0-s${stage}-k2
done
echo "- $(date -u +%FT%TZ) item1 campaign launcher end" >> "$RUNLOG"
