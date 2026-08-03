#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# CP2-A/B plus CP2-C2 unit evidence runner. This program has no bag/dataset path.
set -Eeuo pipefail
umask 077
export PATH=/usr/bin:/bin

# The readiness-barrier self-test is an exclusive, artifact-free surface.  It
# must dispatch before the repository-derived lock, Git status, build/results
# snapshots, or any unit workspace is touched.  The default no-argument mode
# below retains the full CP1/CP2 unit-evidence workflow.
if [[ "$#" -eq 1 && "$1" == "--self-test" ]]; then
    exec /usr/bin/python3 -I -B - <<'PY'
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile

ENTRYPOINT = "scripts/cp2/run_unit_gate.sh"
EXPECTED = (
    "valid_minimal_fixture",
    "cli_exclusivity",
    "forbidden_bag_provider",
    "non_tmp_write",
    "schema_extra_key",
    "schema_missing_key",
    "duplicate_json_key",
    "unsafe_path",
    "symlink",
    "hardlink",
    "manifest_missing_entry",
    "manifest_extra_entry",
    "manifest_digest_mismatch",
    "readiness_order",
    "readiness_timeout",
    "readiness_process_group",
    "readiness_lock_identity",
    "readiness_snapshot_mutation",
    "ignored_source_path",
    "snapshotted_root_symlink",
    "launch_output_combination",
    "unit_anchor_commit_mismatch",
)


class Rejected(ValueError):
    pass


def reject(message):
    raise Rejected(message)


def exact_keys(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys):
        reject("schema key mismatch")


def parse_cli(arguments):
    if tuple(arguments) != ():
        reject("unit actual mode accepts no arguments")


def strict_json(document):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                reject("duplicate JSON key")
            result[key] = value
        return result
    return json.loads(document, object_pairs_hook=unique)


def safe_relpath(value):
    if (not isinstance(value, str) or not value or "\\" in value or "\0" in value
            or os.path.isabs(value) or os.path.normpath(value) != value
            or any(part in ("", ".", "..") for part in value.split("/"))):
        reject("unsafe relative path")


def tmp_child(value, root):
    if not isinstance(value, str) or not os.path.isabs(value):
        reject("write path is not absolute")
    if os.path.commonpath((os.path.normpath(value), root)) != root or os.path.normpath(value) == root:
        reject("write path is not below the temporary root")


def regular_single(path):
    metadata = os.lstat(path)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        reject("not a single-link regular file")


def manifest(expected, observed):
    if set(expected) != set(observed):
        reject("manifest population mismatch")
    if any(observed[path] != digest for path, digest in expected.items()):
        reject("manifest digest mismatch")


def readiness(records):
    if [record.get("index") for record in records] != list(range(len(records))):
        reject("readiness order mismatch")
    if any(record.get("timed_out") is not False for record in records):
        reject("readiness timeout")
    if any(record.get("process_group_complete") is not True for record in records):
        reject("readiness process group incomplete")


def snapshots_equal(before, after):
    if not isinstance(before, bytes) or not isinstance(after, bytes) or before != after:
        reject("readiness snapshots differ")


def lock(record):
    exact_keys(record, ("regular", "owner", "mode"))
    if record != {"regular": True, "owner": True, "mode": 0o600}:
        reject("readiness lock mismatch")


def ignored(path):
    safe_relpath(path)
    if path.split("/", 1)[0] not in ("build", "results", "Testing"):
        reject("ignored source path")


def real_directory(path):
    if not stat.S_ISDIR(os.lstat(path).st_mode):
        reject("snapshotted root symlink")


def launch(record):
    exact_keys(record, ("unit_only", "bag_provider_calls", "artifact_created"))
    if record != {"unit_only": True, "bag_provider_calls": 0, "artifact_created": False}:
        reject("unit self-test launch combination")


def anchor(expected_commit, expected_tree, actual_commit, actual_tree):
    if (expected_commit, expected_tree) != (actual_commit, actual_tree):
        reject("unit anchor identity mismatch")


def run_valid():
    parse_cli(())
    exact_keys({"required": 1}, ("required",))
    strict_json('{"key":1}')
    safe_relpath("logs/self-test.log")
    readiness(({"index": 0, "timed_out": False, "process_group_complete": True},))
    lock({"regular": True, "owner": True, "mode": 0o600})
    ignored("build/self-test")
    launch({"unit_only": True, "bag_provider_calls": 0, "artifact_created": False})
    anchor("a" * 40, "b" * 40, "a" * 40, "b" * 40)


os.umask(0o077)
temporary_root = tempfile.mkdtemp(prefix="schurvio-lite-cp2-unit-self-test-", dir="/tmp")
cases = []
try:
    file_a = os.path.join(temporary_root, "file-a")
    file_b = os.path.join(temporary_root, "file-b")
    link = os.path.join(temporary_root, "link")
    root_link = os.path.join(temporary_root, "root-link")
    with open(file_a, "wb") as stream:
        stream.write(b"a")
    os.link(file_a, file_b)
    os.symlink(file_a, link)
    os.symlink(temporary_root, root_link)
    digest = hashlib.sha256(b"a").hexdigest()

    def forbidden_provider():
        reject("bag provider is forbidden")

    functions = {
        "valid_minimal_fixture": run_valid,
        "cli_exclusivity": lambda: parse_cli(("--self-test", "unexpected")),
        "forbidden_bag_provider": forbidden_provider,
        "non_tmp_write": lambda: tmp_child("/var/tmp/cp2-forbidden", temporary_root),
        "schema_extra_key": lambda: exact_keys({"required": 1, "extra": 2}, ("required",)),
        "schema_missing_key": lambda: exact_keys({}, ("required",)),
        "duplicate_json_key": lambda: strict_json('{"key":1,"key":2}'),
        "unsafe_path": lambda: safe_relpath("../escape"),
        "symlink": lambda: regular_single(link),
        "hardlink": lambda: regular_single(file_a),
        "manifest_missing_entry": lambda: manifest({"a": digest}, {}),
        "manifest_extra_entry": lambda: manifest({"a": digest}, {"a": digest, "b": digest}),
        "manifest_digest_mismatch": lambda: manifest({"a": digest}, {"a": "0" * 64}),
        "readiness_order": lambda: readiness((
            {"index": 1, "timed_out": False, "process_group_complete": True},
            {"index": 0, "timed_out": False, "process_group_complete": True},
        )),
        "readiness_timeout": lambda: readiness((
            {"index": 0, "timed_out": True, "process_group_complete": True},
        )),
        "readiness_process_group": lambda: readiness((
            {"index": 0, "timed_out": False, "process_group_complete": False},
        )),
        "readiness_lock_identity": lambda: lock({"regular": True, "owner": False, "mode": 0o600}),
        "readiness_snapshot_mutation": lambda: snapshots_equal(b"before", b"after"),
        "ignored_source_path": lambda: ignored("cache/file"),
        "snapshotted_root_symlink": lambda: real_directory(root_link),
        "launch_output_combination": lambda: launch(
            {"unit_only": True, "bag_provider_calls": 0, "artifact_created": True}
        ),
        "unit_anchor_commit_mismatch": lambda: anchor("a" * 40, "b" * 40, "c" * 40, "b" * 40),
    }
    if tuple(functions) != EXPECTED:
        raise RuntimeError("self-test inventory differs from its literal declaration")
    for index, name in enumerate(EXPECTED):
        expected_rejection = name != "valid_minimal_fixture"
        observed_rejection = False
        unexpected = False
        try:
            functions[name]()
        except Rejected:
            observed_rejection = True
        except Exception:
            unexpected = True
        cases.append({
            "index": index,
            "name": name,
            "expected_rejection": expected_rejection,
            "observed_rejection": observed_rejection,
            "passed": not unexpected and observed_rejection == expected_rejection,
        })
finally:
    shutil.rmtree(temporary_root)

passed = (not os.path.lexists(temporary_root) and len(cases) == len(EXPECTED)
          and all(case["passed"] for case in cases))
result = {
    "schema_version": 1,
    "record_type": "self_test_result",
    "entrypoint": ENTRYPOINT,
    "temporary_root": temporary_root,
    "bag_provider_calls": 0,
    "cases": cases,
    "case_count": len(cases),
    "passed": passed,
}
print(json.dumps(result, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
raise SystemExit(0 if passed else 1)
PY
fi
if [[ "$#" -ne 0 ]]; then
    echo "CP2 unit gate: expected no arguments or exclusive --self-test" >&2
    exit 2
fi

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
repo_root="$(cd -- "${script_dir}/../.." && pwd -P)"
readonly script_dir repo_root
readonly verifier="${script_dir}/verify_report.py"
readonly ros_setup="/opt/ros/noetic/setup.bash"
readonly repository_build_root="${repo_root}/build"
readonly workspace_parent="${repository_build_root}/cp2-unit-workspaces"
readonly shared_ceres_checkout="${repository_build_root}/vendor/ceres-src"
readonly ceres_commit="facb199f3eda902360f9e1d5271372b7e54febe1"
readonly ceres_tag="1.14.0"
readonly reproducible_prefix="/cp2/reproducible-root"
readonly origin_rpath='$ORIGIN:/opt/ros/noetic/lib'
readonly googletest_source="/usr/src/googletest"
readonly googletest_license="${googletest_source}/googlemock/LICENSE"
readonly googletest_debian_copyright="/usr/share/doc/googletest/copyright"
readonly expected_googletest_archive_sha256="53d536bbe4f5a4007a23ac1abdd58946fe0f0f30e70c2ddd24fd084a789a9b63"
readonly expected_googletest_archive_size_bytes="4454400"
readonly googletest_discovery_line="-- Found gtest sources under '/usr/src/googletest': gtests will be built"
readonly googletest_discovery_stem="Found gtest sources under"
readonly catkin_package_cmake_log_relative="logs/ov_msckf/build.cmake.log"
readonly catkin_package_cmake_log_artifact="catkin_ov_msckf_cmake.log"
readonly artifact_parent="${repo_root}/results/staging/cp2/unit"
readonly lock_root="/tmp"

readonly -a archive_roots=(
    .
    ':(exclude)project/datasets.yaml'
)
readonly -a cp1_tests=(
    test_cp1_schur_equivalence
    test_cp1_rank_rejection
    test_cp1_projection_jacobian
    test_cp1_prior_and_compression
)
readonly -a cp2_tests=(
    test_cp2_production_schur_reducer
    test_cp2_fej_golden
    test_cp2_state_update_semantics
    test_cp2_configuration_contract
    test_cp2_updater_msckf_end_to_end
    test_cp2_updater_msckf_fault_injection
    test_cp2_composite_state
    test_cp2_commit_oracle
    test_cp2_commit_boundary
    test_cp2_canonical
    test_cp2_offline_replay
    test_cp2_recorded_assemble
    test_cp2_feature_gate
    test_cp2_runtime_context
    test_cp2_ros1_runtime_parameters
    test_cp2_serial_pairing
    test_cp2_serial_runtime_trace
    test_cp2_updater_msckf_preview_snapshot
    test_cp2_shadow_math
    test_cp2_trace_journal
    test_cp2_trace_codec
)
readonly -a all_tests=("${cp1_tests[@]}" "${cp2_tests[@]}")
readonly -a eigen_abi_dependency_packages=(
    ov_core
    ov_init
)
readonly -a required_copied_dsos=(
    libov_msckf_lib.so
    libov_core_lib.so
    libov_init_lib.so
    libgtest.so
    libceres.so.1
)

die() {
    echo "CP2 unit gate: $*" >&2
    exit 2
}

utc_now() {
    date -u +%Y-%m-%dT%H:%M:%S.%6NZ
}

sha256_path() {
    sha256sum -- "$1" | awk '{print $1}'
}

tree_metadata_signature() {
    /usr/bin/env -i PATH=/usr/bin:/bin LANG=C LC_ALL=C TZ=UTC PYTHONHASHSEED=0 \
        /usr/bin/python3 -I -B - "$1" <<'PY'
import hashlib
import os
import stat
import sys

root = os.fsencode(sys.argv[1])
if not os.path.lexists(root):
    print("absent")
    raise SystemExit(0)

records = []
pending = [(root, b".")]
while pending:
    path, relative = pending.pop()
    status = os.lstat(path)
    target = os.readlink(path) if stat.S_ISLNK(status.st_mode) else b""
    records.append((
        relative,
        stat.S_IFMT(status.st_mode),
        stat.S_IMODE(status.st_mode),
        status.st_uid,
        status.st_gid,
        status.st_size,
        status.st_mtime_ns,
        status.st_ctime_ns,
        target,
    ))
    if stat.S_ISDIR(status.st_mode):
        with os.scandir(path) as stream:
            children = sorted(
                ((entry.name, path + b"/" + entry.name) for entry in stream),
                key=lambda item: item[0],
                reverse=True,
            )
        for name, child in children:
            child_relative = name if relative == b"." else relative + b"/" + name
            pending.append((child, child_relative))

digest = hashlib.sha256()
for record in sorted(records, key=lambda item: item[0]):
    for value in record:
        encoded = value if isinstance(value, bytes) else str(value).encode("ascii")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
print(digest.hexdigest())
PY
}

# The lock lives outside the repository so --self-test cannot create source,
# build, or results artifacts. Reject the ordinary symlink attack before the
# descriptor is opened; flock then serializes all runners for this repository.
lock_identity="$(printf '%s' "${repo_root}" | sha256sum | awk '{print $1}')"
lock_path="${lock_root}/schurvio-lite-cp2-unit-${lock_identity}.lock"
[[ ! -L "${lock_path}" ]] || die "refusing symlink lock path: ${lock_path}"
exec {cp2_lock_fd}>"${lock_path}"
if ! flock -n "${cp2_lock_fd}"; then
    die "another CP2 unit runner holds the exclusive repository lock"
fi
readonly lock_identity lock_path cp2_lock_fd

runner_self_test() {
    local self_log self_status=0 timestamp_sample
    local status_before status_after build_before build_after results_before results_after
    self_log="$(mktemp --tmpdir cp2-unit-runner-self-test.XXXXXXXX.log)" ||
        die "could not allocate runner self-test log"

    status_before="$(/usr/bin/git -C "${repo_root}" status --porcelain=v1 --untracked-files=all)"
    build_before="$(tree_metadata_signature "${repository_build_root}")"
    results_before="$(tree_metadata_signature "${repo_root}/results")"

    if ! /bin/bash -n -- "${BASH_SOURCE[0]}"; then
        echo "runner shell syntax check failed" >&2
        self_status=1
    fi
    timestamp_sample="$(utc_now)"
    if ! /usr/bin/env -i PATH=/usr/bin:/bin LANG=C LC_ALL=C TZ=UTC PYTHONHASHSEED=0 \
        /usr/bin/python3 -I -B - "${timestamp_sample}" <<'PY'
import datetime
import re
import sys

sample = sys.argv[1]
if re.fullmatch(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z",
    sample,
) is None:
    raise SystemExit("utc_now timestamp is not canonical six-fraction UTC")
parsed = datetime.datetime.fromisoformat(sample[:-1] + "+00:00")
if parsed.tzinfo is None or parsed.utcoffset() != datetime.timedelta(0):
    raise SystemExit("utc_now timestamp did not parse as UTC")
if parsed.isoformat(timespec="microseconds").replace("+00:00", "Z") != sample:
    raise SystemExit("utc_now timestamp does not round-trip canonically")
PY
    then
        echo "runner utc_now timestamp contract check failed: ${timestamp_sample}" >&2
        self_status=1
    fi
    if ! /usr/bin/env -i PATH=/usr/bin:/bin LANG=C LC_ALL=C TZ=UTC \
        /usr/bin/python3 -I -B - "${BASH_SOURCE[0]}" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
in_block = False
start = None
body = []
for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
    if not in_block and "<<'PY'" in line:
        in_block = True
        start = number + 1
        body = []
    elif in_block and line == "PY":
        compile("\n".join(body) + "\n", "{}:heredoc@{}".format(path, start), "exec")
        in_block = False
    elif in_block:
        body.append(line)
if in_block:
    raise SystemExit("unclosed embedded Python heredoc at line {}".format(start))
PY
    then
        echo "runner embedded Python syntax check failed" >&2
        self_status=1
    fi
    if ! grep -Fx -- 'set -Eeuo pipefail' "${BASH_SOURCE[0]}" >/dev/null; then
        echo "runner static invariant is missing: strict shell failure mode" >&2
        self_status=1
    fi
    if ! grep -Eq -- '^/usr/bin/git -c tar\.umask=0002 -C .* archive --format=tar ' "${BASH_SOURCE[0]}"; then
        echo "runner static invariant is missing: exact Git archive source" >&2
        self_status=1
    fi
    if ! grep -F -- "':(exclude)project/datasets.yaml'" "${BASH_SOURCE[0]}" >/dev/null; then
        echo "runner static invariant is missing: preauthorization registry archive exclusion" >&2
        self_status=1
    fi
    if ! grep -Eq -- '^[[:space:]]+/usr/bin/env -i "\$\{test_environment\[@\]\}"' \
        "${BASH_SOURCE[0]}"; then
        echo "runner static invariant is missing: sanitized test execution" >&2
        self_status=1
    fi
    if ! grep -Eq -- '^[[:space:]]+/usr/bin/python3 .* --finalize-staging-noreplace' \
        "${BASH_SOURCE[0]}"; then
        echo "runner static invariant is missing: staging-only no-overwrite finalization" >&2
        self_status=1
    fi
    if ! grep -Eq -- '^if ! run_build_step ceres_configure /usr/bin/cmake ' \
        "${BASH_SOURCE[0]}" ||
       ! grep -Eq -- '^if ! run_build_step ceres_install /usr/bin/cmake --install ' \
        "${BASH_SOURCE[0]}"; then
        echo "runner static invariant is missing: fresh per-workspace Ceres build/install" >&2
        self_status=1
    fi
    if ! grep -F -- 'THIRD_PARTY_NOTICES/Ceres-LICENSE' "${BASH_SOURCE[0]}" >/dev/null ||
       ! grep -F -- 'googletest_source_snapshot.tar' "${BASH_SOURCE[0]}" >/dev/null; then
        echo "runner static invariant is missing: dependency source/notices" >&2
        self_status=1
    fi
    local cp2_c2_target
    for cp2_c2_target in test_cp2_updater_msckf_fault_injection \
                         test_cp2_commit_oracle test_cp2_commit_boundary; do
        if ! grep -Fx -- "    ${cp2_c2_target}" "${BASH_SOURCE[0]}" >/dev/null; then
            echo "runner static invariant is missing CP2-C2 target: ${cp2_c2_target}" >&2
            self_status=1
        fi
    done
    if ! grep -F -- 'link_ov_msckf_cp2_fault_lib.txt' "${BASH_SOURCE[0]}" >/dev/null ||
       ! grep -F -- 'link_test_cp2_updater_msckf_fault_injection.txt' \
           "${BASH_SOURCE[0]}" >/dev/null; then
        echo "runner static invariant is missing: CP2-C2 link-isolation evidence" >&2
        self_status=1
    fi
    local eligibility_phrase='eligible CP2 '"seal"
    if ! grep -F -- "not an ${eligibility_phrase}" "${BASH_SOURCE[0]}" >/dev/null; then
        echo "runner static invariant is missing: explicit CP2 ineligibility" >&2
        self_status=1
    fi
    local forbidden_workspace_token="build/"'cp1'
    if grep -F -- "${forbidden_workspace_token}" "${BASH_SOURCE[0]}" >/dev/null; then
        echo "runner must not reuse the CP1 workspace" >&2
        self_status=1
    fi
    local forbidden_shared_install_token="vendor/"'ceres-install'
    if grep -F -- "${forbidden_shared_install_token}" "${BASH_SOURCE[0]}" >/dev/null; then
        echo "runner must not reuse the shared Ceres install" >&2
        self_status=1
    fi
    if [[ ! -r "${verifier}" ]]; then
        echo "verifier is not readable: ${verifier}" >&2
        self_status=1
    elif ! /usr/bin/env -i \
        PATH=/usr/bin:/bin LANG=C LC_ALL=C TZ=UTC PYTHONHASHSEED=0 \
        /usr/bin/python3 -I -B "${verifier}" --unit-self-test >"${self_log}" 2>&1; then
        sed -n '1,240p' "${self_log}" >&2
        echo "verifier self-test failed" >&2
        self_status=1
    fi

    status_after="$(/usr/bin/git -C "${repo_root}" status --porcelain=v1 --untracked-files=all)"
    build_after="$(tree_metadata_signature "${repository_build_root}")"
    results_after="$(tree_metadata_signature "${repo_root}/results")"
    if [[ "${status_before}" != "${status_after}" || "${build_before}" != "${build_after}" ||
          "${results_before}" != "${results_after}" ]]; then
        echo "runner self-test changed repository/build/results state" >&2
        self_status=1
    fi
    rm -f -- "${self_log}"

    if [[ "${self_status}" -ne 0 ]]; then
        return "${self_status}"
    fi
    echo "CP2 unit runner self-test passed; no repository build/results artifacts were created."
}

case "${1:-}" in
    --self-test)
        [[ "$#" -eq 1 ]] || die "--self-test takes no additional arguments"
        runner_self_test
        exit 0
        ;;
    "")
        ;;
    *)
        die "usage: ${BASH_SOURCE[0]} [--self-test]"
        ;;
esac

for tool in /usr/bin/git /usr/bin/cmake /usr/bin/catkin /usr/bin/python3 \
            /usr/bin/env /usr/bin/flock /usr/bin/tar /usr/bin/find \
            /usr/bin/install /usr/bin/cmp /usr/bin/readelf /usr/bin/ldd /usr/bin/dpkg-query \
            sha256sum awk; do
    if [[ "${tool}" == /* ]]; then
        [[ -x "${tool}" ]] || die "required tool is missing: ${tool}"
    elif ! command -v "${tool}" >/dev/null 2>&1; then
        die "required tool is missing: ${tool}"
    fi
done
[[ -r "${ros_setup}" ]] || die "ROS Noetic setup file is missing: ${ros_setup}"
[[ -r "${verifier}" ]] || die "CP2 verifier is missing: ${verifier}"

# Validate every existing lexical component. A missing suffix is allowed only
# beneath a validated existing directory; callers validate again after mkdir.
validate_canonical_nonsymlink_path() {
    local path="$1"
    /usr/bin/python3 -I -B - "${repo_root}" "${path}" <<'PY'
import os
from pathlib import Path
import stat
import sys

repo = Path(sys.argv[1])
target = Path(sys.argv[2])
if not repo.is_absolute() or not target.is_absolute():
    raise SystemExit("paths must be absolute")
if Path(os.path.realpath(str(repo))) != repo:
    raise SystemExit("repository root is not canonical: " + str(repo))
try:
    target.relative_to(repo)
except ValueError:
    raise SystemExit("path escapes the repository: " + str(target))

current = Path(target.anchor)
for component in target.parts[1:]:
    current = current / component
    if not os.path.lexists(str(current)):
        break
    mode = os.lstat(str(current)).st_mode
    if stat.S_ISLNK(mode):
        raise SystemExit("symlink path component is forbidden: " + str(current))
    if Path(os.path.realpath(str(current))) != current:
        raise SystemExit("noncanonical path component: " + str(current))
PY
}

validate_canonical_nonsymlink_path "${repo_root}"
validate_canonical_nonsymlink_path "${repository_build_root}"
validate_canonical_nonsymlink_path "${workspace_parent}"
validate_canonical_nonsymlink_path "${shared_ceres_checkout}"
validate_canonical_nonsymlink_path "${artifact_parent}"

# Refuse dirty source before touching repository build or results paths.
source_status="$(/usr/bin/git -C "${repo_root}" status --porcelain=v1 --untracked-files=all)"
if [[ -n "${source_status}" ]]; then
    echo "CP2 unit gate refuses dirty source:" >&2
    printf '%s\n' "${source_status}" >&2
    exit 2
fi

source_commit="$(/usr/bin/git -C "${repo_root}" rev-parse --verify HEAD)"
source_tree="$(/usr/bin/git -C "${repo_root}" rev-parse --verify 'HEAD^{tree}')"
source_branch="$(/usr/bin/git -C "${repo_root}" branch --show-current)"
source_epoch="$(/usr/bin/git -C "${repo_root}" show -s --format=%ct "${source_commit}")"
[[ "${source_branch}" == "schurvio-lite/cp2-one-pass" ]] ||
    die "evidence branch must be schurvio-lite/cp2-one-pass (got: ${source_branch:-detached})"
[[ "${source_epoch}" =~ ^[0-9]+$ ]] || die "commit timestamp is invalid"
readonly source_commit source_tree source_branch source_epoch

[[ -d "${shared_ceres_checkout}/.git" ]] ||
    die "pinned Ceres source checkout is missing: ${shared_ceres_checkout}"
actual_ceres_commit="$(/usr/bin/git -C "${shared_ceres_checkout}" rev-parse --verify HEAD)"
actual_ceres_tag="$(/usr/bin/git -C "${shared_ceres_checkout}" describe --tags --exact-match)"
actual_ceres_status="$(/usr/bin/git -C "${shared_ceres_checkout}" status --porcelain=v1 --untracked-files=all)"
[[ "${actual_ceres_commit}" == "${ceres_commit}" && "${actual_ceres_tag}" == "${ceres_tag}" ]] ||
    die "Ceres source is not pinned to ${ceres_tag} at ${ceres_commit}"
[[ -z "${actual_ceres_status}" ]] || die "pinned Ceres source checkout is dirty"
readonly actual_ceres_commit actual_ceres_tag actual_ceres_status

googletest_package_version="$(/usr/bin/dpkg-query -W -f='${Version}' googletest)"
[[ "${googletest_package_version}" == "1.10.0-2" ]] ||
    die "GoogleTest package version differs from the controlled 1.10.0-2 input"
[[ "$(sha256_path "${googletest_license}")" == \
   "9702de7e4117a8e2b20dafab11ffda58c198aede066406496bef670d40a22138" ]] ||
    die "GoogleTest license bytes differ from the pinned notice"
[[ "$(sha256_path "${shared_ceres_checkout}/LICENSE")" == \
   "065e9b9f40b65dfaeb421a8a1c0559d8305e3ce9394aa8b7dec609fa04e8318a" ]] ||
    die "Ceres license bytes differ from the pinned notice"
readonly googletest_package_version

verifier_self_test_log="$(mktemp --tmpdir cp2-verifier-self-test.XXXXXXXX.log)" ||
    die "could not allocate verifier self-test log"
verifier_self_test_started="$(utc_now)"
if /usr/bin/env -i \
    PATH=/usr/bin:/bin LANG=C LC_ALL=C TZ=UTC PYTHONHASHSEED=0 \
    /usr/bin/python3 -I -B "${verifier}" --unit-self-test >"${verifier_self_test_log}" 2>&1; then
    verifier_self_test_status=0
else
    verifier_self_test_status=$?
fi
verifier_self_test_finished="$(utc_now)"
if [[ "${verifier_self_test_status}" -ne 0 ]]; then
    sed -n '1,240p' "${verifier_self_test_log}" >&2
    rm -f -- "${verifier_self_test_log}"
    die "independent verifier self-test failed; no evidence directory was created"
fi
if ! grep -Eq -- '^CP2_READINESS_ENGINE_PROTECTING_TESTS count=39 passed=true module_sha256=[0-9a-f]{64} output_sha256=[0-9a-f]{64}$' \
    "${verifier_self_test_log}"; then
    sed -n '1,240p' "${verifier_self_test_log}" >&2
    rm -f -- "${verifier_self_test_log}"
    die "readiness-engine protecting tests are absent from the evidenced verifier gate"
fi
if ! grep -Eq -- '^CP2_D_DATA_FREE_PROTECTING_TESTS count=66 passed=true module_sha256=[0-9a-f]{64} output_sha256=[0-9a-f]{64}$' \
    "${verifier_self_test_log}"; then
    sed -n '1,320p' "${verifier_self_test_log}" >&2
    rm -f -- "${verifier_self_test_log}"
    die "CP2-D data-free protecting tests are absent from the evidenced verifier gate"
fi
if ! grep -Eq -- '^CP2_E_DATA_FREE_PROTECTING_TESTS count=37 passed=true module_sha256=[0-9a-f]{64} output_sha256=[0-9a-f]{64}$' \
    "${verifier_self_test_log}"; then
    sed -n '1,360p' "${verifier_self_test_log}" >&2
    rm -f -- "${verifier_self_test_log}"
    die "CP2-E data-free protecting tests are absent from the evidenced verifier gate"
fi
readonly verifier_self_test_started verifier_self_test_finished verifier_self_test_status

mkdir -p -- "${workspace_parent}"
validate_canonical_nonsymlink_path "${repository_build_root}"
validate_canonical_nonsymlink_path "${workspace_parent}"

run_stamp="$(date -u +%Y%m%dT%H%M%S%NZ)"
workspace="$(mktemp -d "${workspace_parent}/.cp2-unit-${run_stamp}-g${source_commit:0:12}.workspace.XXXXXXXX")" ||
    die "could not create a unique CP2 workspace"
workspace="$(cd -- "${workspace}" && pwd -P)"
source_space="${workspace}/src"
workspace_build_root="${workspace}/build"
package_build="${workspace_build_root}/ov_msckf"
binary_build_dir="${workspace}/devel/lib/ov_msckf"
task_home_dir="${workspace}/home"
task_tmp_dir="${workspace}/tmp"
source_archive="${workspace}/source_snapshot.tar"
ceres_source="${workspace}/ceres-src"
ceres_build="${workspace}/ceres-build"
ceres_prefix="${workspace}/ceres-install"
ceres_dir="${ceres_prefix}/lib/cmake/Ceres"
ceres_archive="${workspace}/ceres_source_snapshot.tar"
googletest_archive="${workspace}/googletest_source_snapshot.tar"
mkdir -p -- "${source_space}" "${ceres_source}" "${task_home_dir}" "${task_tmp_dir}"
validate_canonical_nonsymlink_path "${workspace}"

run_id="cp2_unit_${run_stamp}-g${source_commit:0:12}-${workspace##*.workspace.}"
run_dir=""
final_dir=""
run_succeeded=0
cleanup() {
    rm -f -- "${verifier_self_test_log}"
    if [[ "${run_succeeded}" -eq 0 && -n "${run_dir}" && -d "${run_dir}" ]]; then
        echo "CP2 diagnostic artifacts remain in the incomplete staging directory:" >&2
        echo "  ${run_dir}" >&2
    fi
    if [[ "${run_succeeded}" -eq 1 && -d "${workspace}" ]]; then
        # A retained extracted tree would introduce untracked .gitignore/
        # .gitattributes controls before C3 readiness.  The finalized unit
        # artifact is self-contained and the hidden verifier is artifact-only.
        chmod -R u+w -- "${workspace}"
        rm -rf -- "${workspace}"
    elif [[ -d "${workspace}" ]]; then
        echo "CP2 fresh build workspace was retained for reproducibility/diagnosis:" >&2
        echo "  ${workspace}" >&2
    fi
}
trap cleanup EXIT

# The compilation source is a fresh, exact Git archive of only the recorded
# package/build-script roots. It has no worktree overlay and is made read-only.
/usr/bin/git -c tar.umask=0002 -C "${repo_root}" archive --format=tar --output="${source_archive}" \
    "${source_commit}" -- "${archive_roots[@]}"
/usr/bin/tar --extract --file "${source_archive}" --directory "${source_space}" \
    --no-same-owner --no-same-permissions
if find "${source_space}" -type l -print -quit | grep -q .; then
    die "source archive unexpectedly contains a symlink"
fi

# Isolated catkin uses Debian's root-owned /usr/src/googletest tree. Retain its
# canonical tar before the build and require an identical canonical tar after.
[[ -d "${googletest_source}" && -r "${googletest_license}" &&
   -r "${googletest_debian_copyright}" ]] ||
    die "controlled GoogleTest 1.10.0 source/license metadata is unavailable"
if find "${googletest_source}" -type l -print -quit | grep -q .; then
    die "controlled GoogleTest source contains a symlink"
fi
if find "${googletest_source}" -writable -print -quit | grep -q .; then
    die "controlled GoogleTest source is writable by the evidence runner"
fi
/usr/bin/tar --sort=name --mtime='@0' --owner=0 --group=0 --numeric-owner \
    --format=gnu --create --file="${googletest_archive}" \
    --directory=/usr/src googletest
find "${source_space}" -exec chmod a-w -- {} +
chmod 0444 -- "${source_archive}"
chmod 0444 -- "${googletest_archive}"
if find "${source_space}" -perm /0222 -print -quit | grep -q .; then
    die "source archive snapshot is not read-only"
fi
source_read_only_before_build=true
archive_sha256="$(sha256_path "${source_archive}")"
archive_size_bytes="$(stat -c %s -- "${source_archive}")"
googletest_archive_sha256="$(sha256_path "${googletest_archive}")"
googletest_archive_size_bytes="$(stat -c %s -- "${googletest_archive}")"
[[ "${googletest_archive_sha256}" == "${expected_googletest_archive_sha256}" &&
   "${googletest_archive_size_bytes}" == "${expected_googletest_archive_size_bytes}" ]] ||
    die "GoogleTest source archive differs from the pinned 1.10.0-2 bytes"

/usr/bin/git -C "${shared_ceres_checkout}" archive --format=tar \
    --output="${ceres_archive}" "${ceres_commit}"
/usr/bin/tar --extract --file "${ceres_archive}" --directory "${ceres_source}" \
    --no-same-owner --no-same-permissions
if find "${ceres_source}" -type l -print -quit | grep -q .; then
    die "pinned Ceres archive unexpectedly contains a symlink"
fi
find "${ceres_source}" -exec chmod a-w -- {} +
chmod 0444 -- "${ceres_archive}"
if find "${ceres_source}" -perm /0222 -print -quit | grep -q .; then
    die "pinned Ceres source archive snapshot is not read-only"
fi
ceres_archive_sha256="$(sha256_path "${ceres_archive}")"
ceres_archive_size_bytes="$(stat -c %s -- "${ceres_archive}")"

readonly workspace source_space workspace_build_root package_build binary_build_dir
readonly task_home_dir task_tmp_dir source_archive archive_sha256 archive_size_bytes
readonly ceres_source ceres_build ceres_prefix ceres_dir ceres_archive
readonly ceres_archive_sha256 ceres_archive_size_bytes
readonly googletest_archive googletest_archive_sha256 googletest_archive_size_bytes

mkdir -p -- "${artifact_parent}"
validate_canonical_nonsymlink_path "${artifact_parent}"
run_dir="$(mktemp -d "${artifact_parent}/.${run_id}.partial.XXXXXXXX")" ||
    die "could not create atomic staging directory"
run_dir="$(cd -- "${run_dir}" && pwd -P)"
final_dir="${artifact_parent}/${run_id}"
validate_canonical_nonsymlink_path "${run_dir}"
validate_canonical_nonsymlink_path "${final_dir}"
[[ ! -e "${final_dir}" && ! -L "${final_dir}" ]] ||
    die "no-overwrite final staging path already exists: ${final_dir}"
mkdir -p -- "${run_dir}/binaries"
mkdir -p -- "${run_dir}/THIRD_PARTY_NOTICES"
install -m 0444 -- "${source_archive}" "${run_dir}/source_snapshot.tar"
install -m 0444 -- "${ceres_archive}" "${run_dir}/ceres_source_snapshot.tar"
install -m 0444 -- "${googletest_archive}" "${run_dir}/googletest_source_snapshot.tar"
install -m 0444 -- "${ceres_source}/LICENSE" \
    "${run_dir}/THIRD_PARTY_NOTICES/Ceres-LICENSE"
install -m 0444 -- "${googletest_license}" \
    "${run_dir}/THIRD_PARTY_NOTICES/GoogleTest-LICENSE"
install -m 0444 -- "${verifier_self_test_log}" "${run_dir}/verifier_self_test.log"

readonly -a build_environment=(
    "CC=/usr/bin/cc"
    "CMAKE_PREFIX_PATH=/opt/ros/noetic"
    "CXX=/usr/bin/c++"
    "HOME=${task_home_dir}"
    "LANG=C"
    "LC_ALL=C"
    "LD_LIBRARY_PATH=/opt/ros/noetic/lib"
    "LOGNAME=moksh"
    "PATH=/usr/bin:/bin"
    "PKG_CONFIG_PATH=/opt/ros/noetic/lib/pkgconfig"
    "PYTHONHASHSEED=0"
    "PYTHONPATH=/opt/ros/noetic/lib/python3/dist-packages"
    "ROSLISP_PACKAGE_DIRECTORIES="
    "ROS_DISTRO=noetic"
    "ROS_ETC_DIR=/opt/ros/noetic/etc/ros"
    "ROS_MASTER_URI=http://localhost:11311"
    "ROS_PACKAGE_PATH=/opt/ros/noetic/share"
    "ROS_PYTHON_VERSION=3"
    "ROS_ROOT=/opt/ros/noetic/share/ros"
    "ROS_VERSION=1"
    "SOURCE_DATE_EPOCH=${source_epoch}"
    "TMPDIR=${task_tmp_dir}"
    "TZ=UTC"
    "USER=moksh"
)
readonly -a test_environment=(
    "LANG=C"
    "LC_ALL=C"
    "LD_LIBRARY_PATH=binaries:/opt/ros/noetic/lib"
    "MKL_NUM_THREADS=1"
    "OMP_NUM_THREADS=1"
    "OPENBLAS_NUM_THREADS=1"
    "PATH=/usr/bin:/bin"
    "TZ=UTC"
)
readonly -a test_unset_environment=(
    CPATH
    CPLUS_INCLUDE_PATH
    GTEST_ALSO_RUN_DISABLED_TESTS
    GTEST_BREAK_ON_FAILURE
    GTEST_BRIEF
    GTEST_CATCH_EXCEPTIONS
    GTEST_COLOR
    GTEST_FAIL_FAST
    GTEST_FILTER
    GTEST_LIST_TESTS
    GTEST_OUTPUT
    GTEST_PRINT_TIME
    GTEST_RANDOM_SEED
    GTEST_REPEAT
    GTEST_SHARD_INDEX
    GTEST_SHUFFLE
    GTEST_THROW_ON_FAILURE
    GTEST_TOTAL_SHARDS
    LD_PRELOAD
    LIBRARY_PATH
    PYTHONPATH
)
readonly deterministic_compile_flags="-ffile-prefix-map=\"${workspace}\"=${reproducible_prefix} -fdebug-prefix-map=\"${workspace}\"=${reproducible_prefix} -fmacro-prefix-map=\"${workspace}\"=${reproducible_prefix}"
readonly deterministic_link_flags="-Wl,--build-id=sha1"

write_source_snapshot() {
    local destination="$1"
    /usr/bin/python3 -I -B - "${repo_root}" "${destination}" <<'PY'
import datetime
import json
import os
from pathlib import Path
import subprocess
import sys

repo = Path(sys.argv[1]).resolve()
destination = Path(sys.argv[2])

def git(*args):
    return subprocess.check_output(["/usr/bin/git", *args], cwd=str(repo), text=True).rstrip("\n")

record = {
    "branch": git("branch", "--show-current"),
    "commit": git("rev-parse", "--verify", "HEAD"),
    "recorded_utc": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
    "status_porcelain_v1": git("status", "--porcelain=v1", "--untracked-files=all").splitlines(),
    "tree": git("rev-parse", "--verify", "HEAD^{tree}"),
}
temporary = destination.with_name("." + destination.name + ".tmp." + str(os.getpid()))
with temporary.open("x", encoding="utf-8") as stream:
    json.dump(record, stream, indent=2, sort_keys=True)
    stream.write("\n")
    stream.flush()
    os.fsync(stream.fileno())
os.replace(str(temporary), str(destination))
PY
}

environment_json() {
    local include_unset="$1"
    shift
    /usr/bin/python3 -I -B - "${include_unset}" "$@" <<'PY'
import json
import sys

include_unset = int(sys.argv[1])
values = {}
for item in sys.argv[2:]:
    if "=" in item:
        key, value = item.split("=", 1)
        values[key] = value
    elif include_unset:
        values[item] = "unset"
    else:
        raise SystemExit("environment item lacks '=': " + item)
print(json.dumps(values, sort_keys=True, separators=(",", ":")))
PY
}

build_environment_json="$(environment_json 0 "${build_environment[@]}")"
test_environment_json="$(environment_json 1 "${test_environment[@]}" "${test_unset_environment[@]}")"
verifier_environment_json='{"LANG":"C","LC_ALL":"C","PATH":"/usr/bin:/bin","PYTHONHASHSEED":"0","TZ":"UTC"}'
readonly build_environment_json test_environment_json verifier_environment_json

write_source_snapshot "${run_dir}/source_before.json"

/usr/bin/python3 -I -B - "${run_dir}/verifier_self_test.json" "${verifier}" \
    "${verifier_self_test_started}" "${verifier_self_test_finished}" \
    "${verifier_self_test_status}" "${verifier_environment_json}" <<'PY'
import json
import os
from pathlib import Path
import sys

destination = Path(sys.argv[1])
record = {
    "argv": ["/usr/bin/python3", "-I", "-B", sys.argv[2], "--unit-self-test"],
    "cwd": ".",
    "environment": json.loads(sys.argv[6]),
    "environment_mode": "env-i",
    "exit_status": int(sys.argv[5]),
    "finished_utc": sys.argv[4],
    "name": "verifier_self_test",
    "started_utc": sys.argv[3],
    "synthetic_artifact_root_policy": "tempfile.TemporaryDirectory_outside_repository_results",
}
temporary = destination.with_name("." + destination.name + ".tmp." + str(os.getpid()))
with temporary.open("x", encoding="utf-8") as stream:
    json.dump(record, stream, indent=2, sort_keys=True)
    stream.write("\n")
    stream.flush()
    os.fsync(stream.fileno())
os.replace(str(temporary), str(destination))
PY

write_workspace_record() {
    local read_only_after="$1"
    local ceres_read_only_after="$2"
    /usr/bin/python3 -I -B - "${run_dir}/workspace.json" "${repo_root}" \
        "${repository_build_root}" "${workspace}" "${workspace_build_root}" \
        "${source_space}" "${source_commit}" "${source_tree}" \
        "${archive_sha256}" "${archive_size_bytes}" "${read_only_after}" \
        "${ceres_source}" "${ceres_build}" "${ceres_prefix}" \
        "${ceres_commit}" "${ceres_tag}" "${ceres_archive_sha256}" \
        "${ceres_archive_size_bytes}" "${ceres_read_only_after}" \
        "${googletest_archive_sha256}" "${googletest_archive_size_bytes}" \
        "${googletest_archive_sha256_after_build}" \
        "${googletest_archive_size_bytes_after_build}" \
        "${reproducible_prefix}" "${origin_rpath}" \
        "${archive_roots[@]}" <<'PY'
import datetime
import hashlib
import json
import os
from pathlib import Path
import sys

destination = Path(sys.argv[1])
roots = sys.argv[26:]
archive_path = Path(sys.argv[4]) / "source_snapshot.tar"
ceres_archive_path = Path(sys.argv[4]) / "ceres_source_snapshot.tar"
googletest_archive_path = Path(sys.argv[4]) / "googletest_source_snapshot.tar"
record = {
    "archive_argv": [
        "/usr/bin/git", "-c", "tar.umask=0002", "-C", sys.argv[2],
        "archive", "--format=tar",
        "--output=" + str(archive_path), sys.argv[7], "--", *roots,
    ],
    "archive_artifact": "source_snapshot.tar",
    "archive_roots": roots,
    "archive_sha256": sys.argv[9],
    "archive_size_bytes": int(sys.argv[10]),
    "ceres_archive_argv": [
        "/usr/bin/git", "-C", str(Path(sys.argv[3]) / "vendor/ceres-src"),
        "archive", "--format=tar", "--output=" + str(ceres_archive_path), sys.argv[15],
    ],
    "ceres_archive_artifact": "ceres_source_snapshot.tar",
    "ceres_archive_sha256": sys.argv[17],
    "ceres_archive_size_bytes": int(sys.argv[18]),
    "ceres_build_root": sys.argv[13],
    "ceres_install_prefix": sys.argv[14],
    "ceres_source_commit": sys.argv[15],
    "ceres_source_read_only_after_build": sys.argv[19] == "true",
    "ceres_source_read_only_before_build": True,
    "ceres_source_root": sys.argv[12],
    "ceres_source_tag": sys.argv[16],
    "created_utc": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
    "fresh": True,
    "googletest_archive_argv": [
        "/usr/bin/tar", "--sort=name", "--mtime=@0", "--owner=0", "--group=0",
        "--numeric-owner", "--format=gnu", "--create",
        "--file=" + str(googletest_archive_path), "--directory=/usr/src", "googletest",
    ],
    "googletest_archive_artifact": "googletest_source_snapshot.tar",
    "googletest_archive_sha256": sys.argv[20],
    "googletest_archive_sha256_after_build": sys.argv[22],
    "googletest_archive_sha256_before_build": sys.argv[20],
    "googletest_archive_size_bytes": int(sys.argv[21]),
    "googletest_archive_size_bytes_after_build": int(sys.argv[23]),
    "googletest_archive_size_bytes_before_build": int(sys.argv[21]),
    "googletest_source_root": "/usr/src/googletest",
    "googletest_unchanged_after_build": (
        sys.argv[20] == sys.argv[22] and int(sys.argv[21]) == int(sys.argv[23])
    ),
    "kind": "unique_git_archive_cp2_catkin_workspace",
    "repo_root": sys.argv[2],
    "repository_build_root": sys.argv[3],
    "reused": False,
    "reproducible_prefix": sys.argv[24],
    "runtime_rpath": sys.argv[25],
    "schema_version": 1,
    "source_commit": sys.argv[7],
    "source_read_only_after_build": sys.argv[11] == "true",
    "source_read_only_before_build": True,
    "source_root": sys.argv[6],
    "source_tree": sys.argv[8],
    "workspace": sys.argv[4],
    "workspace_build_root": sys.argv[5],
}
temporary = destination.with_name("." + destination.name + ".tmp." + str(os.getpid()))
with temporary.open("x", encoding="utf-8") as stream:
    json.dump(record, stream, indent=2, sort_keys=True)
    stream.write("\n")
    stream.flush()
    os.fsync(stream.fileno())
os.replace(str(temporary), str(destination))
PY
}

write_command_record() {
    local destination="$1"
    local name="$2"
    local exit_status="$3"
    local started_utc="$4"
    local finished_utc="$5"
    local environment="$6"
    shift 6
    /usr/bin/python3 -I -B - "${destination}" "${name}" "${exit_status}" \
        "${started_utc}" "${finished_utc}" "${environment}" "$@" <<'PY'
import json
import os
from pathlib import Path
import sys

destination = Path(sys.argv[1])
record = {
    "argv": sys.argv[7:],
    "cwd": ".",
    "environment": json.loads(sys.argv[6]),
    "environment_mode": "env-i",
    "exit_status": int(sys.argv[3]),
    "finished_utc": sys.argv[5],
    "name": sys.argv[2],
    "serialized": True,
    "started_utc": sys.argv[4],
}
temporary = destination.with_name("." + destination.name + ".tmp." + str(os.getpid()))
with temporary.open("x", encoding="utf-8") as stream:
    json.dump(record, stream, indent=2, sort_keys=True)
    stream.write("\n")
    stream.flush()
    os.fsync(stream.fileno())
os.replace(str(temporary), str(destination))
PY
}

run_build_step() {
    local name="$1"
    shift
    local log_path="${run_dir}/build_${name}.log"
    local temporary_log="${log_path}.tmp.$$"
    local started_utc finished_utc exit_status
    started_utc="$(utc_now)"
    if (
        cd -- "${repo_root}"
        printf '$ cd %q\n' "${repo_root}"
        printf '$ /usr/bin/env -i'
        printf ' %q' "${build_environment[@]}"
        printf ' %q' "$@"
        printf '\n'
        /usr/bin/env -i "${build_environment[@]}" "$@"
    ) >"${temporary_log}" 2>&1; then
        exit_status=0
    else
        exit_status=$?
    fi
    finished_utc="$(utc_now)"
    mv -- "${temporary_log}" "${log_path}"
    write_command_record "${run_dir}/build_${name}.json" "${name}" "${exit_status}" \
        "${started_utc}" "${finished_utc}" "${build_environment_json}" "$@"
    return "${exit_status}"
}

build_failures=0
if ! run_build_step ceres_configure /usr/bin/cmake \
    -S "${ceres_source}" -B "${ceres_build}" \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_VERBOSE_MAKEFILE=ON \
    "-DCMAKE_INSTALL_PREFIX=${ceres_prefix}" \
    -DBUILD_SHARED_LIBS=ON \
    -DBUILD_TESTING=OFF \
    -DBUILD_EXAMPLES=OFF \
    -DBUILD_DOCUMENTATION=OFF \
    -DMINIGLOG=ON \
    -DGFLAGS=OFF \
    -DLAPACK=OFF \
    -DSUITESPARSE=OFF \
    -DCXSPARSE=OFF \
    -DCUSTOM_BLAS=ON \
    "-DCMAKE_C_FLAGS=${deterministic_compile_flags}" \
    "-DCMAKE_CXX_FLAGS=${deterministic_compile_flags}" \
    "-DCMAKE_EXE_LINKER_FLAGS=${deterministic_link_flags}" \
    "-DCMAKE_SHARED_LINKER_FLAGS=${deterministic_link_flags}" \
    -DCMAKE_BUILD_WITH_INSTALL_RPATH=ON \
    "-DCMAKE_BUILD_RPATH=${origin_rpath}" \
    "-DCMAKE_INSTALL_RPATH=${origin_rpath}" \
    -DCMAKE_INSTALL_RPATH_USE_LINK_PATH=FALSE; then
    build_failures=$((build_failures + 1))
fi
if ! run_build_step ceres_build /usr/bin/cmake --build "${ceres_build}" \
    --parallel 1 --verbose; then
    build_failures=$((build_failures + 1))
fi
if ! run_build_step ceres_install /usr/bin/cmake --install "${ceres_build}"; then
    build_failures=$((build_failures + 1))
fi
if ! run_build_step catkin_config /usr/bin/catkin config --workspace "${workspace}" \
    --source-space "${source_space}" --build-space "${workspace_build_root}" \
    --devel-space "${workspace}/devel" --log-space "${workspace}/logs" \
    --extend /opt/ros/noetic --merge-devel --cmake-args \
    -DCMAKE_BUILD_TYPE=RelWithDebInfo \
    -DCMAKE_VERBOSE_MAKEFILE=ON \
    -DCATKIN_ENABLE_TESTING=ON \
    -DBUILD_TESTING=ON \
    -DDISABLE_MATPLOTLIB=ON \
    -DPYTHON_EXECUTABLE=/usr/bin/python3 \
    -DPython_EXECUTABLE=/usr/bin/python3 \
    -DCMAKE_EXPORT_COMPILE_COMMANDS=ON \
    "-DCMAKE_C_FLAGS=${deterministic_compile_flags}" \
    "-DCMAKE_CXX_FLAGS=${deterministic_compile_flags}" \
    "-DCMAKE_EXE_LINKER_FLAGS=${deterministic_link_flags}" \
    "-DCMAKE_SHARED_LINKER_FLAGS=${deterministic_link_flags}" \
    -DCMAKE_BUILD_WITH_INSTALL_RPATH=ON \
    "-DCMAKE_BUILD_RPATH=${origin_rpath}" \
    "-DCMAKE_INSTALL_RPATH=${origin_rpath}" \
    -DCMAKE_INSTALL_RPATH_USE_LINK_PATH=FALSE \
    "-DCeres_DIR=${ceres_dir}"; then
    build_failures=$((build_failures + 1))
fi
if ! run_build_step catkin_build /usr/bin/catkin build --workspace "${workspace}" \
    ov_msckf --jobs 1 --no-status --summarize; then
    build_failures=$((build_failures + 1))
fi
if ! run_build_step test_targets_build /usr/bin/cmake --build "${package_build}" --target \
    "${all_tests[@]}" -- -j1 VERBOSE=1; then
    build_failures=$((build_failures + 1))
fi

catkin_package_cmake_log_source="${workspace}/${catkin_package_cmake_log_relative}"
catkin_package_cmake_log_destination="${run_dir}/${catkin_package_cmake_log_artifact}"
[[ -f "${catkin_package_cmake_log_source}" && ! -L "${catkin_package_cmake_log_source}" ]] ||
    die "Catkin package configure log is missing or nonregular: ${catkin_package_cmake_log_source}"
[[ "$(grep -Fxc -- "${googletest_discovery_line}" \
        "${catkin_package_cmake_log_source}")" == "1" &&
   "$(grep -Fc -- "${googletest_discovery_stem}" \
        "${catkin_package_cmake_log_source}")" == "1" ]] ||
    die "Catkin package configure log does not contain exactly one exact GoogleTest discovery line"
install -m 0444 -- "${catkin_package_cmake_log_source}" \
    "${catkin_package_cmake_log_destination}"
/usr/bin/cmp -s -- "${catkin_package_cmake_log_source}" \
    "${catkin_package_cmake_log_destination}" ||
    die "retained Catkin package configure log differs from the workspace source log"
[[ "$(grep -Fxc -- "${googletest_discovery_line}" \
        "${catkin_package_cmake_log_destination}")" == "1" &&
   "$(grep -Fc -- "${googletest_discovery_stem}" \
        "${catkin_package_cmake_log_destination}")" == "1" ]] ||
    die "retained Catkin package configure log lost the exact GoogleTest discovery line"
readonly catkin_package_cmake_log_source catkin_package_cmake_log_destination

if [[ "${build_failures}" -eq 0 ]]; then
    expected_gtest_source="gtest_SOURCE_DIR:STATIC=/usr/src/googletest/googletest"
    grep -Fx -- "${expected_gtest_source}" "${package_build}/CMakeCache.txt" >/dev/null ||
        die "CMakeCache does not bind GoogleTest to the retained Debian source"
fi

if [[ -f "${package_build}/compile_commands.json" ]]; then
    install -m 0444 -- "${package_build}/compile_commands.json" "${run_dir}/compile_commands.json"
fi
declare -A cp2_link_command_sources=(
    [link_ov_msckf_lib.txt]="${package_build}/CMakeFiles/ov_msckf_lib.dir/link.txt"
    [link_ov_msckf_cp2_fault_lib.txt]="${package_build}/CMakeFiles/ov_msckf_cp2_fault_lib.dir/link.txt"
    [link_test_cp2_updater_msckf_end_to_end.txt]="${package_build}/CMakeFiles/test_cp2_updater_msckf_end_to_end.dir/link.txt"
    [link_test_cp2_updater_msckf_fault_injection.txt]="${package_build}/CMakeFiles/test_cp2_updater_msckf_fault_injection.dir/link.txt"
)
for link_artifact in "${!cp2_link_command_sources[@]}"; do
    link_source="${cp2_link_command_sources[${link_artifact}]}"
    if [[ -f "${link_source}" && ! -L "${link_source}" ]]; then
        install -m 0444 -- "${link_source}" "${run_dir}/${link_artifact}"
    fi
done
unset link_artifact link_source
for dependency_package in "${eigen_abi_dependency_packages[@]}"; do
    dependency_compile_commands="${workspace_build_root}/${dependency_package}/compile_commands.json"
    if [[ -f "${dependency_compile_commands}" && ! -L "${dependency_compile_commands}" ]]; then
        install -m 0444 -- "${dependency_compile_commands}" \
            "${run_dir}/compile_commands_${dependency_package}.json"
    fi
done
unset dependency_package dependency_compile_commands
if [[ -f "${package_build}/CMakeCache.txt" ]]; then
    install -m 0444 -- "${package_build}/CMakeCache.txt" "${run_dir}/CMakeCache.txt"
fi

# Snapshot the complete non-system runtime closure produced by this workspace:
# all three project DSOs, the workspace-built gtest DSO, and exact Ceres SONAME.
declare -A dso_sources=(
    [libov_msckf_lib.so]="${workspace}/devel/lib/libov_msckf_lib.so"
    [libov_core_lib.so]="${workspace}/devel/lib/libov_core_lib.so"
    [libov_init_lib.so]="${workspace}/devel/lib/libov_init_lib.so"
    [libgtest.so]="${package_build}/gtest/lib/libgtest.so"
    [libceres.so.1]="${ceres_prefix}/lib/libceres.so.1"
)
if [[ "${build_failures}" -eq 0 ]]; then
    /usr/bin/python3 -I -B - "${workspace}" <<'PY'
from pathlib import Path
import sys

workspace = Path(sys.argv[1])
expected = {
    "libgtest.so",
    "libov_core_lib.so",
    "libov_init_lib.so",
    "libov_msckf_lib.so",
}
actual = {
    path.name
    for root in (workspace / "devel", workspace / "build")
    for path in root.rglob("*.so*")
    if path.is_file()
}
if actual != expected:
    raise SystemExit(
        "fresh workspace DSO inventory mismatch: expected {} got {}".format(
            sorted(expected), sorted(actual)
        )
    )
PY
    for dso_name in "${required_copied_dsos[@]}"; do
        dso_source="${dso_sources[${dso_name}]}"
        [[ -f "${dso_source}" ]] || die "required runtime DSO is missing: ${dso_source}"
        install -m 0555 -- "${dso_source}" "${run_dir}/binaries/${dso_name}"
    done
fi

for test_name in "${all_tests[@]}"; do
    source_binary="${binary_build_dir}/${test_name}"
    if [[ "${build_failures}" -eq 0 ]]; then
        [[ -x "${source_binary}" ]] || die "required test executable is missing: ${source_binary}"
        install -m 0555 -- "${source_binary}" "${run_dir}/binaries/${test_name}"
    fi
done

capture_loader_map() {
    local test_name="$1"
    local destination="$2"
    local ldd_output="${run_dir}/.${test_name}.ldd.tmp.$$"
    if (
        cd -- "${run_dir}"
        /usr/bin/env -i "${test_environment[@]}" /usr/bin/ldd "binaries/${test_name}"
    ) >"${ldd_output}" 2>&1; then
        :
    else
        local ldd_status=$?
        sed -n '1,240p' "${ldd_output}" >&2
        return "${ldd_status}"
    fi
    /usr/bin/python3 -I -B - "${run_dir}" "${ldd_output}" "${destination}" <<'PY'
import hashlib
import json
import os
from pathlib import Path
import re
import sys

root = Path(sys.argv[1]).resolve()
lines = Path(sys.argv[2]).read_text(encoding="utf-8", errors="strict").splitlines()
destination = Path(sys.argv[3])

def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()

result = {}
for raw in lines:
    line = raw.strip()
    if not line:
        continue
    if " => not found" in line:
        raise SystemExit("unresolved loader dependency: " + line)
    if " => " in line:
        soname, remainder = line.split(" => ", 1)
        resolved = remainder.rsplit(" (", 1)[0]
    elif line.startswith("linux-vdso"):
        soname = line.split(None, 1)[0]
        result[soname] = {"artifact_path": None, "resolved_path": "linux-vdso", "sha256": None}
        continue
    else:
        resolved = line.rsplit(" (", 1)[0]
        soname = Path(resolved).name
    path = Path(resolved)
    if not path.is_absolute():
        path = root / path
    canonical = path.resolve(strict=True)
    try:
        artifact_path = canonical.relative_to(root).as_posix()
        resolved_record = artifact_path
    except ValueError:
        artifact_path = None
        resolved_record = str(canonical)
        allowed = False
        for allowed_root in (Path("/opt/ros/noetic"), Path("/usr/lib"), Path("/lib")):
            try:
                canonical.relative_to(allowed_root.resolve())
                allowed = True
                break
            except ValueError:
                pass
        if not allowed:
            raise SystemExit("loader dependency escapes artifact/system/ROS roots: " + str(canonical))
    if soname in result:
        raise SystemExit("duplicate SONAME in loader map: " + soname)
    result[soname] = {
        "artifact_path": artifact_path,
        "resolved_path": resolved_record,
        "sha256": digest(canonical),
    }
temporary = destination.with_name("." + destination.name + ".tmp." + str(os.getpid()))
with temporary.open("x", encoding="utf-8") as stream:
    json.dump(result, stream, indent=2, sort_keys=True)
    stream.write("\n")
    stream.flush()
    os.fsync(stream.fileno())
os.replace(str(temporary), str(destination))
PY
    rm -f -- "${ldd_output}"
}

execution_inputs_json() {
    local test_name="$1"
    /usr/bin/python3 -I -B - "${run_dir}" "${test_name}" "${required_copied_dsos[@]}" <<'PY'
import hashlib
import json
from pathlib import Path
import sys

root = Path(sys.argv[1])
paths = ["binaries/" + sys.argv[2]] + ["binaries/" + name for name in sys.argv[3:]]
result = {}
for relative in paths:
    path = root / relative
    if not path.is_file():
        raise SystemExit("missing execution input: " + relative)
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    result[relative] = h.hexdigest()
print(json.dumps(result, sort_keys=True, separators=(",", ":")))
PY
}

write_test_record() {
    local destination="$1"
    local name="$2"
    local checkpoint="$3"
    local exit_status="$4"
    local started_utc="$5"
    local finished_utc="$6"
    local before_json="$7"
    local after_json="$8"
    local loader_file="$9"
    /usr/bin/python3 -I -B - "${destination}" "${name}" "${checkpoint}" "${exit_status}" \
        "${started_utc}" "${finished_utc}" "${test_environment_json}" \
        "${before_json}" "${after_json}" "${loader_file}" <<'PY'
import json
import os
from pathlib import Path
import sys

destination = Path(sys.argv[1])
name = sys.argv[2]
record = {
    "argv": [
        "binaries/" + name,
        "--gtest_color=no",
        "--gtest_filter=*",
        "--gtest_repeat=1",
        "--gtest_shuffle=0",
        "--gtest_output=xml:" + name + ".xml",
    ],
    "checkpoint": sys.argv[3],
    "cwd": ".",
    "environment": json.loads(sys.argv[7]),
    "environment_mode": "env-i",
    "execution_inputs_sha256_after": json.loads(sys.argv[9]),
    "execution_inputs_sha256_before": json.loads(sys.argv[8]),
    "exit_status": int(sys.argv[4]),
    "finished_utc": sys.argv[6],
    "loader_map": json.loads(Path(sys.argv[10]).read_text(encoding="utf-8")),
    "name": name,
    "serialized": True,
    "started_utc": sys.argv[5],
}
temporary = destination.with_name("." + destination.name + ".tmp." + str(os.getpid()))
with temporary.open("x", encoding="utf-8") as stream:
    json.dump(record, stream, indent=2, sort_keys=True)
    stream.write("\n")
    stream.flush()
    os.fsync(stream.fileno())
os.replace(str(temporary), str(destination))
PY
}

test_failures=0
declare -A loader_map_files=()
for test_name in "${all_tests[@]}"; do
    checkpoint="CP2"
    if [[ "${test_name}" == test_cp1_* ]]; then
        checkpoint="CP1"
    fi
    log_path="${run_dir}/${test_name}.log"
    temporary_log="${log_path}.tmp.$$"
    loader_file="${run_dir}/.${test_name}.loader.tmp.json"
    loader_map_files[${test_name}]="${loader_file}"
    started_utc="$(utc_now)"
    if [[ "${build_failures}" -eq 0 ]]; then
        capture_loader_map "${test_name}" "${loader_file}"
        before_json="$(execution_inputs_json "${test_name}")"
        if (
            cd -- "${run_dir}"
            /usr/bin/env -i "${test_environment[@]}" \
                "binaries/${test_name}" \
                --gtest_color=no \
                '--gtest_filter=*' \
                --gtest_repeat=1 \
                --gtest_shuffle=0 \
                "--gtest_output=xml:${test_name}.xml"
        ) >"${temporary_log}" 2>&1; then
            test_status=0
        else
            test_status=$?
        fi
        mv -- "${temporary_log}" "${log_path}"
        after_json="$(execution_inputs_json "${test_name}")"
    else
        test_status=127
        before_json='{}'
        after_json='{}'
        printf 'NOT RUN: clean serialized build did not produce the required runtime closure.\n' \
            >"${log_path}"
        printf '{}\n' >"${loader_file}"
    fi
    finished_utc="$(utc_now)"
    write_test_record "${run_dir}/${test_name}.json" "${test_name}" "${checkpoint}" \
        "${test_status}" "${started_utc}" "${finished_utc}" "${before_json}" \
        "${after_json}" "${loader_file}"
    if [[ "${before_json}" != "${after_json}" || "${test_status}" -ne 0 ]]; then
        test_failures=$((test_failures + 1))
    fi
done

googletest_after_archive="${workspace}/googletest_source_after.tar"
/usr/bin/tar --sort=name --mtime='@0' --owner=0 --group=0 --numeric-owner \
    --format=gnu --create --file="${googletest_after_archive}" \
    --directory=/usr/src googletest
googletest_archive_sha256_after_build="$(sha256_path "${googletest_after_archive}")"
googletest_archive_size_bytes_after_build="$(stat -c %s -- "${googletest_after_archive}")"
[[ "${googletest_archive_sha256_after_build}" == "${googletest_archive_sha256}" &&
   "${googletest_archive_size_bytes_after_build}" == "${googletest_archive_size_bytes}" ]] ||
    die "system GoogleTest source changed during the controlled build"
rm -f -- "${googletest_after_archive}"
readonly googletest_archive_sha256_after_build googletest_archive_size_bytes_after_build

/usr/bin/python3 -I -B - "${run_dir}/dependency_inventory.json" "${run_dir}" \
    "${workspace}" "${package_build}" "${ceres_source}" "${ceres_prefix}" \
    "${ceres_commit}" "${ceres_tag}" "${ceres_archive_sha256}" \
    "${googletest_archive_sha256}" "${googletest_archive_size_bytes}" \
    "${googletest_archive_sha256_after_build}" \
    "${googletest_archive_size_bytes_after_build}" \
    "${googletest_package_version}" "${googletest_license}" \
    "${googletest_debian_copyright}" \
    "${required_copied_dsos[@]}" -- "${all_tests[@]}" <<'PY'
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

destination = Path(sys.argv[1])
root = Path(sys.argv[2])
workspace = Path(sys.argv[3])
package_build = Path(sys.argv[4])
ceres_source = Path(sys.argv[5])
ceres_prefix = Path(sys.argv[6])
ceres_commit = sys.argv[7]
ceres_tag = sys.argv[8]
ceres_archive_sha256 = sys.argv[9]
googletest_archive_sha256 = sys.argv[10]
googletest_archive_size_bytes = int(sys.argv[11])
googletest_archive_sha256_after_build = sys.argv[12]
googletest_archive_size_bytes_after_build = int(sys.argv[13])
googletest_package_version = sys.argv[14]
googletest_license = Path(sys.argv[15])
googletest_debian_copyright = Path(sys.argv[16])
separator = sys.argv.index("--")
required = sys.argv[17:separator]
tests = sys.argv[separator + 1:]

def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()

sources = {
    "libov_msckf_lib.so": workspace / "devel/lib/libov_msckf_lib.so",
    "libov_core_lib.so": workspace / "devel/lib/libov_core_lib.so",
    "libov_init_lib.so": workspace / "devel/lib/libov_init_lib.so",
    "libgtest.so": package_build / "gtest/lib/libgtest.so",
    "libceres.so.1": ceres_prefix / "lib/libceres.so.1",
}
test_sources = {name: workspace / "devel/lib/ov_msckf" / name for name in tests}

def readelf_details(path):
    dynamic = subprocess.check_output(
        ["/usr/bin/readelf", "-d", str(path)], text=True,
        env={"LANG": "C", "LC_ALL": "C", "PATH": "/usr/bin:/bin"},
    )
    notes = subprocess.check_output(
        ["/usr/bin/readelf", "-n", str(path)], text=True,
        env={"LANG": "C", "LC_ALL": "C", "PATH": "/usr/bin:/bin"},
    )
    sonames = re.findall(r"Library soname: \[([^]]+)\]", dynamic)
    rpath = re.findall(r"Library rpath: \[([^]]*)\]", dynamic)
    runpath = re.findall(r"Library runpath: \[([^]]*)\]", dynamic)
    build_ids = re.findall(r"Build ID: ([0-9a-f]+)", notes)
    if len(build_ids) != 1:
        raise SystemExit("ELF lacks exactly one GNU build ID: " + str(path))
    for value in rpath + runpath:
        for component in value.split(":"):
            if component not in {"$ORIGIN", "/opt/ros/noetic/lib"}:
                raise SystemExit("ELF has escaping RPATH/RUNPATH: {}: {}".format(path, value))
    return {
        "build_id": build_ids[0],
        "rpath": rpath,
        "runpath": runpath,
        "soname": sonames[0] if len(sonames) == 1 else None,
    }

def copied_entry(name, source_path, require_soname):
    artifact_path = root / "binaries" / name
    if not artifact_path.is_file() or not source_path.is_file():
        return {
            "artifact_path": "binaries/" + name, "artifact_sha256": None,
            "build_id": None, "exact_copy": False, "mode": None, "rpath": [],
            "runpath": [], "size_bytes": None, "soname": None,
            "source_path": str(source_path), "source_sha256": None,
        }
    canonical_source = source_path.resolve(strict=True)
    source_sha = digest(canonical_source)
    artifact_sha = digest(artifact_path)
    details = readelf_details(artifact_path)
    if source_sha != artifact_sha:
        raise SystemExit("artifact differs from fresh build output: " + name)
    if require_soname and details["soname"] != name:
        raise SystemExit("DSO SONAME differs from copied name: " + name)
    return {
        "artifact_path": "binaries/" + name,
        "artifact_sha256": artifact_sha,
        "build_id": details["build_id"],
        "exact_copy": True,
        "mode": format(stat.S_IMODE(artifact_path.stat().st_mode), "04o"),
        "rpath": details["rpath"],
        "runpath": details["runpath"],
        "size_bytes": artifact_path.stat().st_size,
        "soname": details["soname"],
        "source_path": str(canonical_source),
        "source_sha256": source_sha,
    }

copied = [copied_entry(name, sources[name], True) for name in required]
copied_tests = [copied_entry(name, test_sources[name], False) for name in tests]
loader_maps = {}
for name in tests:
    loader_file = root / ("." + name + ".loader.tmp.json")
    loader_maps[name] = json.loads(loader_file.read_text(encoding="utf-8"))
ceres_library = sources["libceres.so.1"].resolve() if sources["libceres.so.1"].exists() else sources["libceres.so.1"]
notice_paths = {
    "ceres": root / "THIRD_PARTY_NOTICES/Ceres-LICENSE",
    "googletest": root / "THIRD_PARTY_NOTICES/GoogleTest-LICENSE",
}
record = {
    "ceres": {
        "archive_artifact": "ceres_source_snapshot.tar",
        "archive_sha256": ceres_archive_sha256,
        "snapshotted_soname": "libceres.so.1",
        "source_checkout": str(ceres_source.resolve()) if ceres_source.exists() else str(ceres_source),
        "source_commit": ceres_commit,
        "source_library_path": str(ceres_library),
        "source_library_sha256": digest(ceres_library) if ceres_library.is_file() else None,
        "source_tag": ceres_tag,
    },
    "copied_dsos": copied,
    "copied_test_executables": copied_tests,
    "distribution_status": "internal_non_conveyable_staging",
    "googletest": {
        "archive_artifact": "googletest_source_snapshot.tar",
        "archive_sha256_after_build": googletest_archive_sha256_after_build,
        "archive_sha256_before_build": googletest_archive_sha256,
        "archive_size_bytes_after_build": googletest_archive_size_bytes_after_build,
        "archive_size_bytes_before_build": googletest_archive_size_bytes,
        "build_source_root": "/usr/src/googletest",
        "debian_copyright_path": str(googletest_debian_copyright),
        "debian_copyright_sha256": digest(googletest_debian_copyright),
        "package": "googletest",
        "package_version": googletest_package_version,
        "source_root": "/usr/src/googletest",
        "unchanged_after_build": (
            googletest_archive_sha256_after_build == googletest_archive_sha256
            and googletest_archive_size_bytes_after_build == googletest_archive_size_bytes
        ),
    },
    "independent_source_to_binary_attestation": False,
    "loader_maps": loader_maps,
    "required_copied_dsos": required,
    "schema_version": 1,
    "third_party_notices": {
        "ceres": {
            "artifact_path": "THIRD_PARTY_NOTICES/Ceres-LICENSE",
            "component_version": "1.14.0",
            "sha256": digest(notice_paths["ceres"]),
            "source_path": str(ceres_source / "LICENSE"),
        },
        "googletest": {
            "artifact_path": "THIRD_PARTY_NOTICES/GoogleTest-LICENSE",
            "component_version": googletest_package_version,
            "sha256": digest(notice_paths["googletest"]),
            "source_path": str(googletest_license),
        },
    },
    "threat_model": "trusted_runner_local_staging",
}
temporary = destination.with_name("." + destination.name + ".tmp." + str(os.getpid()))
with temporary.open("x", encoding="utf-8") as stream:
    json.dump(record, stream, indent=2, sort_keys=True)
    stream.write("\n")
    stream.flush()
    os.fsync(stream.fileno())
os.replace(str(temporary), str(destination))
PY
for test_name in "${all_tests[@]}"; do
    rm -f -- "${loader_map_files[${test_name}]}"
done

if find "${source_space}" -perm /0222 -print -quit | grep -q .; then
    source_read_only_after_build=false
else
    source_read_only_after_build=true
fi
if find "${ceres_source}" -perm /0222 -print -quit | grep -q .; then
    ceres_read_only_after_build=false
else
    ceres_read_only_after_build=true
fi
write_workspace_record "${source_read_only_after_build}" "${ceres_read_only_after_build}"
write_source_snapshot "${run_dir}/source_after.json"

# Assembly writes staging evidence only. CP2-C2 is a unit sub-gate, not CP2-C;
# the CP2-C3/C/D/E entry points and their self-tests remain incomplete.
if ! /usr/bin/env -i PATH=/usr/bin:/bin LANG=C LC_ALL=C TZ=UTC PYTHONHASHSEED=0 \
    /usr/bin/python3 -I -B "${verifier}" --assemble-unit "${run_dir}" "${repo_root}"; then
    die "could not assemble the CP2-A/B plus CP2-C2 staging report; partial artifacts were retained"
fi
manifest_sha256="$(sha256_path "${run_dir}/SHA256SUMS")"

if ! /usr/bin/env -i PATH=/usr/bin:/bin LANG=C LC_ALL=C TZ=UTC PYTHONHASHSEED=0 \
    /usr/bin/python3 -I -B "${verifier}" --finalize-staging-noreplace \
    "${run_dir}" "${final_dir}"; then
    die "atomic no-overwrite staging finalization failed; partial artifacts were retained"
fi
run_dir=""

if /usr/bin/env -i PATH=/usr/bin:/bin LANG=C LC_ALL=C TZ=UTC PYTHONHASHSEED=0 \
    /usr/bin/python3 -I -B "${verifier}" --expected-manifest-sha256 "${manifest_sha256}" \
    "${final_dir}" "${repo_root}"; then
    verify_status=0
else
    verify_status=$?
fi
if [[ "${verify_status}" -ne 0 || "${build_failures}" -ne 0 || "${test_failures}" -ne 0 ]]; then
    echo "CP2-A/B plus CP2-C2 FAILED; diagnostic staging evidence is retained without overwrite:" >&2
    echo "  ${final_dir}" >&2
    exit 1
fi

run_succeeded=1
echo "CP2-A/B plus CP2-C2 unit evidence passed and is retained as staging:"
echo "  ${final_dir}"
echo "SHA256SUMS SHA-256 external anchor: ${manifest_sha256}"
echo "Evidence class: trusted_runner_local_staging; independent source-to-binary attestation: false."
echo "Distribution status: internal_non_conveyable_staging."
echo "This artifact is not an eligible CP2 seal. CP2-C2 is unit-only;" \
    "CP2-C3, CP2-C, CP2-D, and CP2-E remain unexecuted and unpassed."
