#!/usr/bin/env bash
# Build the C8 fault-injection shim with the same ABI-relevant flags as ov_msckf_lib.
set -euo pipefail
R=/home/moksh/schurvio-lite-rotation-robustness
cd "$(dirname "$0")"
c++ -std=c++14 -O2 -g -DNDEBUG -fPIC -shared -o libc8_faultinj_shim.so c8_faultinj_shim.cpp \
  -fno-fast-math -ffp-contract=off -fsigned-zeros \
  -DEIGEN_DONT_VECTORIZE=1 -DEIGEN_MAX_ALIGN_BYTES=16 -DEIGEN_MAX_STATIC_ALIGN_BYTES=16 -DROS_AVAILABLE=1 \
  -I$R/ov_msckf/src -I$R/ov_core/src -I$R/ov_init/src -I/usr/include/eigen3 -I/opt/ros/noetic/include \
  -isystem /usr/include/opencv4 -I$R/build/vendor/ceres-install/include \
  -Wl,-z,lazy -Wl,--no-undefined -ldl -lcrypto -lstdc++
sha256sum libc8_faultinj_shim.so
