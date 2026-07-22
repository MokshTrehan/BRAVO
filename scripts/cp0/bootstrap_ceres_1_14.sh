#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
repo_root="$(cd -- "${script_dir}/../.." && pwd -P)"

readonly ceres_url="https://github.com/ceres-solver/ceres-solver.git"
readonly ceres_tag="1.14.0"
readonly ceres_commit="facb199f3eda902360f9e1d5271372b7e54febe1"
readonly vendor_root="${repo_root}/build/vendor"
readonly source_dir="${vendor_root}/ceres-src"
readonly build_dir="${vendor_root}/ceres-build"
readonly install_dir="${vendor_root}/ceres-install"

jobs="${CP0_BUILD_JOBS:-$(getconf _NPROCESSORS_ONLN)}"
if [[ ! "${jobs}" =~ ^[1-9][0-9]*$ ]]; then
    echo "CP0_BUILD_JOBS must be a positive integer (got: ${jobs})" >&2
    exit 2
fi

for tool in git cmake; do
    if ! command -v "${tool}" >/dev/null 2>&1; then
        echo "required build tool is missing: ${tool}" >&2
        exit 2
    fi
done

mkdir -p -- "${vendor_root}"

created_source=0
if [[ ! -e "${source_dir}" ]]; then
    git clone --filter=blob:none --no-checkout -- "${ceres_url}" "${source_dir}"
    created_source=1
elif [[ ! -d "${source_dir}/.git" ]]; then
    echo "refusing to replace non-git path: ${source_dir}" >&2
    exit 2
fi

current_commit="$(git -C "${source_dir}" rev-parse HEAD 2>/dev/null || true)"
if [[ "${current_commit}" != "${ceres_commit}" ]]; then
    if [[ "${created_source}" -eq 0 ]] &&
       [[ -n "$(git -C "${source_dir}" status --porcelain --untracked-files=all 2>/dev/null || true)" ]]; then
        echo "Ceres source has local changes; refusing to change its checkout" >&2
        exit 2
    fi
    if ! git -C "${source_dir}" cat-file -e "${ceres_commit}^{commit}" 2>/dev/null; then
        git -C "${source_dir}" fetch --depth=1 origin "${ceres_commit}"
    fi
    git -C "${source_dir}" -c advice.detachedHead=false checkout --detach "${ceres_commit}"
fi
if [[ -n "$(git -C "${source_dir}" status --porcelain --untracked-files=all 2>/dev/null || true)" ]]; then
    echo "Ceres source has local changes; refusing to build an unidentified dependency" >&2
    exit 2
fi

actual_commit="$(git -C "${source_dir}" rev-parse HEAD)"
actual_tag="$(git -C "${source_dir}" describe --tags --exact-match 2>/dev/null || true)"
if [[ "${actual_commit}" != "${ceres_commit}" || "${actual_tag}" != "${ceres_tag}" ]]; then
    echo "Ceres checkout does not match ${ceres_tag} at ${ceres_commit}" >&2
    exit 2
fi

cmake -S "${source_dir}" -B "${build_dir}" \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX="${install_dir}" \
    -DBUILD_SHARED_LIBS=ON \
    -DBUILD_TESTING=OFF \
    -DBUILD_EXAMPLES=OFF \
    -DBUILD_DOCUMENTATION=OFF \
    -DMINIGLOG=ON \
    -DGFLAGS=OFF \
    -DLAPACK=OFF \
    -DSUITESPARSE=OFF \
    -DCXSPARSE=OFF \
    -DCUSTOM_BLAS=ON
cmake --build "${build_dir}" --parallel "${jobs}"
cmake --install "${build_dir}"

version_header="${install_dir}/include/ceres/version.h"
config_file="${install_dir}/lib/cmake/Ceres/CeresConfig.cmake"
library_file="${install_dir}/lib/libceres.so.1.14.0"
for artifact in "${version_header}" "${config_file}" "${library_file}"; do
    if [[ ! -s "${artifact}" ]]; then
        echo "Ceres installation is incomplete: ${artifact}" >&2
        exit 1
    fi
done

if ! grep -Eq '^#define CERES_VERSION_MAJOR 1$' "${version_header}" ||
   ! grep -Eq '^#define CERES_VERSION_MINOR 14$' "${version_header}"; then
    echo "installed Ceres headers do not report version 1.14" >&2
    exit 1
fi

echo "Ceres ${ceres_tag} (${ceres_commit}) is installed locally at:"
echo "  ${install_dir}"
