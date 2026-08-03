# CP2 artifact and runner schema

Status: **CP2-C detached-readiness replacement draft pending explicit approval;
CP2-D/E evidence remains blocked; no dataset access authorized**

This schema is subordinate to `docs/cp2_one_pass_contract.md` and
`docs/cp2_recorded_evidence_contract.md`. The working-tree replacement of its
CP2-C readiness section is non-authorizing until the exact commit containing
it and
`docs/cp2_c_detached_readiness_binding_clarification_proposed.md` receives the
required explicit approval. Once approved, it freezes the CP2-C machine
interface and the common readiness barrier that must run before a bag path is
resolved, hashed, imported through `rosbag`, or opened. The CP2-D section
preregisters its stricter sequence interface for the next hard checkpoint,
but its runner and verifier stop before readiness/artifact access while the
evaluator-precision/provenance and direct numerical-stack replacement in
`docs/cp2_d_evaluator_precision_clarification_proposed.md` is pending.
CP2-E remains a non-authorizing draft until its fixed-clock profile and complete
machine schema are separately committed; no text in its section can make a
timing artifact eligible before then.

While the CP2-C replacement remains pending, the actual recorded runner is
hard blocked before readiness or registry access and the actual recorded
artifact verifier is hard blocked before artifact access. The implementation
and synthetic/unit verifier paths remain reviewable, but no CP2-C recorded
evidence can be created or accepted by this source state.

## Strict data conventions

All JSON is UTF-8 with duplicate object keys forbidden. `NaN`, positive or
negative infinity, and non-JSON constants are forbidden. Every object has the
exact keys declared here and rejects additional keys. JSONL has one complete
object and one trailing newline per physical line. Empty lines are forbidden.

The type names below mean:

- `u64`: JSON integer in `[0,2^64-1]`;
- `i64`: JSON integer in `[-2^63,2^63-1]`;
- `f64`: finite JSON number parsed as binary64;
- `sha256`: lowercase 64-character hexadecimal string;
- `utc`: canonical `YYYY-MM-DDTHH:MM:SS.ffffffZ` string;
- `relpath`: normalized nonempty POSIX relative path with no `.` or `..`
  component; and
- `nullable(T)`: either JSON null or exactly type `T`.

Every schema-1 object starts with `"schema_version":1`. All identifiers and
array indices are integers, not decimal strings. A Boolean is never accepted
as an integer. Enum spellings are case-sensitive.

## Exact entry-point inventory and CLI

The only preregistered entry points are:

1. `scripts/cp2/run_unit_gate.sh`;
2. `scripts/cp2/run_recorded_parity.py`;
3. `scripts/cp2/run_sequence_pair.py`;
4. `scripts/cp2/run_timing_pair.py`; and
5. `scripts/cp2/verify_report.py`.

Each must be a committed regular nonsymlink file in `HEAD`. Self-test is an
exclusive mode for every entry point: exactly one `--self-test` argument and
no positional or other option is allowed.

Actual-mode CLIs are exact:

```text
scripts/cp2/run_unit_gate.sh
/usr/bin/python3 -I -B scripts/cp2/run_recorded_parity.py \
  --unit-artifact ABS_PATH --unit-manifest-sha256 SHA256 [--run-id SAFE_ID]
/usr/bin/python3 -I -B scripts/cp2/run_sequence_pair.py \
  --unit-artifact ABS_PATH --unit-manifest-sha256 SHA256 \
  --sequence {MH_01_easy,MH_03_medium,V1_01_easy} [--run-id SAFE_ID]
/usr/bin/python3 -I -B scripts/cp2/run_timing_pair.py \
  --unit-artifact ABS_PATH --unit-manifest-sha256 SHA256 [--run-id SAFE_ID]
```

`SAFE_ID` matches `[A-Za-z0-9][A-Za-z0-9._-]{0,127}`. Actual mode exposes no
bag, offset, config, mode, order, tolerance, quantile, alignment, affinity,
clock, or output-directory override. Those values come only from committed
frozen project files. Every ROS run uses exactly
`project/cp2_serial.launch` at SHA-256
`bede519721575d769a1fcef67c5527661cb39ba05ab77faa6c20771b0756c49a`;
no other launch surface is eligible. Static config, bag, and ground-truth
hashes are the exact values in `project/cp2_gate.yaml`. The verifier retains its existing unit-artifact modes
and adds these mutually exclusive actual modes:

```text
/usr/bin/python3 -I -B scripts/cp2/verify_report.py \
  --verify-recorded ABS_PATH --manifest-sha256 SHA256
/usr/bin/python3 -I -B scripts/cp2/verify_report.py \
  --verify-sequence ABS_PATH --manifest-sha256 SHA256
/usr/bin/python3 -I -B scripts/cp2/verify_report.py \
  --verify-timing ABS_PATH --manifest-sha256 SHA256
/usr/bin/python3 -I -B scripts/cp2/verify_report.py \
  --verify-sequence-set ABS0 ABS1 ABS2 \
  --manifest-sha256 SHA256_0 SHA256_1 SHA256_2
```

For `--verify-recorded`, the verifier invokes the exact runtime executable in
its mutually exclusive offline mode:

```text
ABS_ROS1_SERIAL_MSCKF --cp2-offline-replay ABS_ARTIFACT \
  --output ABS_REPLAY_REPORT
```

Offline replay dispatches before ROS initialization, project/dataset registry
loading, or any bag-provider import and rejects every other argument. Its
output is a precreated regular nonsymlink file under the verifier's fresh
`/tmp` root. It reports zero bag-provider calls and uses the same strict-FP
translation units as the recorded run.

Every ROS run receives one committed-launch `cp2_context_path` naming a
precreated regular nonsymlink JSON file inside that run's hidden partial
directory. Its exact keys are `schema_version`, `record_type`
=`cp2_runtime_context`, `checkpoint`, `run_id`, `sequence_index`,
`sequence_id`, `mode`, `shadow_enabled`, `trace_level`, `source_commit`,
`config_sha256`, `bag_sha256`, `pair_index_sha256`,
`resolved_parameters_sha256`, `trace_directory`, `serial_trace_path`,
`callback_trace_path`, `trajectory_trace_path`, `updater_trace_path`,
`state_payload_path`, `proposal_payload_path`, `raw_system_payload_path`, and
`timing_trace_path`, `runtime_parameters_path`, `loader_map_before_path`,
`loader_map_after_path`, `legacy_state_path`, `legacy_deviation_path`, and
`legacy_timing_path`. Every nonnull hash is sha256, sequence index is u64, flags are
Boolean, and every nonnull path is an absolute normalized nonsymlink child of
`trace_directory`; the directory itself is an absolute normalized child of the
same hidden partial root. `run_id` is SAFE_ID and source commit is 40 lowercase
hexadecimal characters.

`pair_index_sha256` is exactly null in this immutable pre-run context. The
complete serial/pair trace does not exist yet, so placing its eventual digest
in the context would create a causal cycle. After the run, deterministic
artifact assembly computes the ordinary SHA-256 of the complete retained
serial/pair file and places that nonnull digest in the checkpoint report and
every final record that requires `pair_index_sha256`; it does not rewrite the
runtime context. For CP2-C it equals `cp2_report.serial_pairs_sha256`. The
verifier rejects a nonnull pre-run value or any report/final-record digest that
differs from the retained file.

The exact trace-level combinations are those in the recorded-evidence
contract. Runtime-parameter and both loader-map paths are nonnull at every
level. `recorded_full` has nonnull serial, updater and all three payload paths,
with callback, trajectory, timing, and all three legacy-output paths null;
the launch sets both legacy-output Booleans false. `sequence` has nonnull
serial, callback, trajectory and all three legacy-output paths, with
updater/payload/timing null; the launch sets both legacy-output Booleans true.
`timing` has nonnull serial, callback, updater and timing paths, with trajectory,
all three full payload paths and all three legacy-output paths null; the launch
sets both legacy-output Booleans false. Every nonnull legacy path exactly equals
its resolved launch parameter. The context is parsed and its combination,
source/config/input identities, output containment, and SHA-256 are validated
before any bag object is constructed. It is retained in provenance; a missing,
additional, or inconsistent key is fatal rather than a request for default
behavior.

Each run has a distinct staging `trace_directory`, and every nonnull child path
within one context is distinct. These absolute names are immutable runtime
sink names, not final-artifact relpaths; they are expected to become stale
after the hidden partial directory is renamed. Before finalization, the trusted
runner parses every sink and deterministically assembles the declared final
files, then removes the staging sinks. For CP2-C, each serial sink contains
only final-schema `serial_pair` rows for that sequence and the final
`serial_pairs.jsonl` is their byte concatenation in sequence-index order. Each
updater sink is a staging event journal whose typed events are split into the
four final updater/feature/block JSONL files, joined to the completed serial
digest, and emitted in their schema-defined identity order. Each binary sink
has the declared 32-byte file header; the assembler parses its frames and
emits one campaign header followed by the globally ordered frames. Runtime
parameter and loader-map sinks are retained under the corresponding
configuration/runtime provenance records. At `sequence` level, the three
legacy sinks become exactly the mode-prefixed state, deviation, and OpenVINS
timing files declared by CP2-D. No context sink is itself authorized as an
extra final file, no sink may alias another, and assembled bytes are verified
before a staging sink is removed.

## Mandatory pre-bag readiness barrier

Every actual CP2-C/D/E runner executes these steps in order. Failure stops
before dataset access and leaves no result directory.

1. Parse and validate only the CLI. Dispatch `--self-test` before loading a
   project registry, resolving a bag path, or importing a module capable of
   bag access.
2. Require branch `schurvio-lite/cp2-one-pass`, a clean worktree including
   untracked files, a current commit descended from the CP1 authorization, and
   the exact five-entry-point inventory above. Record each entry point's Git
   blob ID, SHA-256, type, and mode.
3. Snapshot source, `build`, `results`, and root `Testing` state. The source snapshot is Git
   status, `HEAD`, tree ID, index tree, and SHA-256 of tracked and untracked
   regular-file `(path,type,mode,size,sha256)` records. The
   `build`/`results`/`Testing`
   snapshots recursively retain the same tuple for every entry and reject
   devices, sockets, and FIFOs; existing symlinks are recorded without
   following them.
4. Run all five self-tests sequentially, each with a 300-second process-group
   timeout, under `/usr/bin/env -i` with only
   `PATH=/usr/bin:/bin`, `LANG=C.UTF-8`, `LC_ALL=C.UTF-8`,
   `CP2_SELF_TEST=1`, and `CP2_FORBID_BAG_ACCESS=1`. Shell and Python entry
   points use their committed isolated shebang or `/usr/bin/python3 -I -B`,
   respectively. Isolated mode must be established by the interpreter before
   startup imports, removes the script/current directory and user site from
   `sys.path`, ignores `PYTHONPATH`, and is checked by each Python entry point;
   Python also sets `sys.dont_write_bytecode=true` before any local import.
   Retain argv, entry-point SHA-256, UTC interval, exit code, timeout flag, and
   stdout/stderr SHA-256. Every self-test reports a zero bag-provider call
   count and uses only a fresh `/tmp` directory. Its final stdout line is the
   exact self-test result object defined below; the barrier parses it and
   rejects a missing, duplicate, extra, skipped, or false case.
5. Require byte-identical source/build/results/Testing snapshots after all
   self-tests and repeat the ignored-path rejection.
6. Open exactly `/tmp/schurvio-lite-cp2-data.lock` with
   `O_CREAT|O_RDWR|O_CLOEXEC` and mode `0600`, require a regular nonsymlink file
   owned by the effective user, and acquire `fcntl.flock(LOCK_EX|LOCK_NB)`.
   Contention fails immediately. The barrier precedes the lock so a self-test
   cannot recursively deadlock. Hold the descriptor through finalization.
7. Recheck `HEAD`, clean status, tree ID, all five entry-point identities, and
   the ignored-path rejection after acquiring the lock.
8. Independently verify the supplied CP2-A/B unit artifact and external
   `SHA256SUMS` digest. Its tested commit and tree must equal the current clean
   runtime commit and tree; its complete CP1/CP2 suite, strict floating-point
   evidence, executable/DSO provenance, and corruption self-test must pass.
9. Only now may the runner read `project/datasets.yaml`, resolve or stat a bag,
   import/use `rosbag`, hash input bytes, or launch a process that opens a bag.

Subject to the replacement approval above, the barrier record has exact keys:
`schema_version`, `record_type`=`readiness_barrier`, `entrypoints` (five
records), `source_before_sha256`, `build_before_sha256`,
`source_before_payload`, `build_before_payload`, `results_before_payload`,
`results_before_sha256`, `testing_before_payload`, `testing_before_sha256`,
`self_tests` (five records in inventory order),
`source_after_payload`, `source_after_sha256`, `build_after_payload`,
`build_after_sha256`, `results_after_payload`, `results_after_sha256`,
`testing_after_payload`, `testing_after_sha256`,
`post_lock_payload`, `post_lock_recheck_sha256`, `post_unit_payload`,
`post_unit_recheck_sha256`, `source_context_payload`,
`source_context_sha256`, `data_lock`, `readiness_git_environment`,
`readiness_git_commands`, `unit_verification`, `unit_artifact`, `unit_manifest_sha256`,
`unit_tested_commit`, `unit_tested_tree`, `bag_provider_calls` (zero), and
`passed` (true). Each entry-point record has exact keys `path`, `git_blob`,
`sha256`, `mode`, and `regular_nonsymlink`. Each self-test record has exact keys
`index`, `path`, `argv`, `cwd`, `entrypoint_sha256`, `started_utc`, `finished_utc`,
`environment_sha256`, `exit_code`, `timed_out`, `process_group_complete`,
`stdout`, `stdout_sha256`, `stderr`, `stderr_sha256`,
`expected_case_names`, `result`, `bag_provider_calls`, and
`temporary_root_removed`. `cwd` is exactly `/tmp`,
`process_group_complete` and `temporary_root_removed` are true, and `stdout` and
`stderr` are distinct relpaths retained in the eventual artifact;
`expected_case_names` is the exact ordered nonempty name array embedded in the
committed entry point, and `result` is the parsed final-line object. Its case
names/order must equal that array, in addition to containing the mandatory
subset below.

Every readiness snapshot is retained as canonical bytes under
`readiness/` and hashed directly. Its domain is the ASCII bytes
`SchurVIO-CP2-readiness-snapshot-v1` plus a zero byte, followed by a
length-prefixed root tag (`source`, `build`, `results`, `testing`, or
`post_lock`), one
existence byte, and a u64 big-endian entry count. Entries are sorted strictly
by UTF-8 relative-path bytes and encode: length-prefixed path, one type byte
(`f` regular file, `d` directory, or `l` symlink), u64 big-endian mode, u64
big-endian size, and 32 raw SHA-256 bytes. A directory has zero size and an
all-zero hash. A symlink's size and hash cover its raw link-target bytes and it
is never followed. The root itself is not an entry. An absent
`build`/`results`/`Testing` root has existence byte zero and zero entries. An
existing one of those three roots must itself be a real nonsymlink directory
before its children are encoded. Source entries
are exactly the union produced by
`git ls-files --cached --others --exclude-standard -z`, excluding the
separately snapshotted `build`, `results`, and `Testing` roots; every listed path must be a
regular nonsymlink file. Source modes are normalized to the Git index mode
whose encoded u64 is exactly octal `0100644` (decimal 33188) or octal `0100755`
(decimal 33261); build/results/Testing modes are the u64 value
`lstat().st_mode & 07777`. Every entry-point record's JSON `mode` is the same
full Git-mode u64, never a string or permission-only value.

Before any workspace-local Python import, the bootstrap also enumerates
`git ls-files --others --ignored --exclude-standard -z`. Every ignored path
outside the three exact root prefixes `build/`, `results/`, and `Testing/` is
fatal; directories are judged by their first path component. Thus ignored
`__pycache__`, `.pyc`, editor output, or alternate build trees cannot be read or
mutated outside a snapshotted root. Each Python entry point's precheck path may
import only the standard library until this rejection and the entry-point
identity check have passed.
Source and post-lock payloads then append, in order, 20 raw bytes each for
`HEAD`, source tree and index tree, a length-prefixed exact
porcelain-v1-with-untracked status
byte string, and the five entry-point path/blob/SHA records in inventory order.
Invalid/nonhex Git identities fail before encoding. The before/after/post-lock
payload relpaths and hashes are included in the barrier record; the verifier
parses and rehashes every payload, requires byte-identical before/after payloads
for each root, and reconstructs the source/post-lock payload from the retained
source archive. `source_context_payload` retains the exact canonical JSON bytes
fed to the independent unit-anchor verifier at readiness step 8; its digest is
`source_context_sha256`, and the detached actual verifier revalidates that
context against the postauthorization source archive, governing approvals,
entry points, contracts, and unit anchor. The separate `post_unit` payload is a
second `post_lock`-tag snapshot captured after detached unit verification and
must be byte-identical to the retained post-lock payload.

`data_lock` has exact keys `path`, `device`, `inode`, `mode`, `owner_uid`,
`link_count`, and `acquired_exclusive`. Its path is exactly
`/tmp/schurvio-lite-cp2-data.lock`; the remaining integer fields retain the
held descriptor's `st_dev`, `st_ino`, permission-only mode `0600`, `st_uid`,
and `st_nlink` respectively, and `acquired_exclusive` is true. The producer
checks those values against the held descriptor and live path throughout the
authorized run. Historical detached verification validates the retained exact
types, path, mode, link count, and nonzero inode; it does not require the
process-global lock path to preserve one ephemeral inode after the runner
exits.

`readiness_git_environment` is the exact `readiness_git_v1` environment-class
object `{environment_id,variables,canonical_sha256}`. Its variables are the
bytewise-name-sorted complete environment used by every readiness Git child:
`GIT_ALLOW_PROTOCOL=none`, `GIT_ATTR_NOSYSTEM=1`,
`GIT_CONFIG_NOSYSTEM=1`, `GIT_LITERAL_PATHSPECS=1`,
`GIT_NO_LAZY_FETCH=1`, `GIT_NO_REPLACE_OBJECTS=1`,
`GIT_OPTIONAL_LOCKS=0`, `GIT_PAGER=` (empty),
`GIT_PROTOCOL_FROM_USER=0`, `GIT_TERMINAL_PROMPT=0`,
`LANG=C.UTF-8`, `LC_ALL=C.UTF-8`, `PATH=/usr/bin:/bin`, and private
`HOME`/`TMPDIR` directories named `git-home`/`git-tmp` beneath the same fresh
`/tmp/schurvio-cp2-readiness-*` root. The producer requires those directories
to be distinct real mode-0700 directories while readiness is live. Historical
verification retains and validates their exact lexical relationship and does
not require the intentionally removed private tree to be recreated.

`readiness_git_commands` is exactly 20 records, indexed contiguously. Each has
exact keys `command_index`, `argv`, `cwd`, `environment_sha256`,
`stdin_sha256`, `stdout_sha256`, `stderr_sha256`, `stdout_payload`,
`started_utc`, `finished_utc`, `exit_code`, `timed_out`, and
`process_group_complete`. The exact command suffixes are, in order: initial
`ls-files --stage -z`; `ls-files --others -z`; `cat-file --batch`; `status
--porcelain=v1 -z --untracked-files=all`; `ls-files --others --ignored
--exclude-standard -z`; `rev-parse --verify HEAD`; `rev-parse --verify
HEAD^{tree}`; `merge-base --is-ancestor APPROVED_CP1_COMMIT HEAD`; then the
status/HEAD/tree triplet four times for source-before, source-after, post-lock,
and post-unit capture. Every argv starts with the frozen absolute Git binary,
descriptor-bound Git/worktree arguments, and exact fail-closed `-c` options;
`cwd` is `/proc/self/fd/3`; stderr is empty; exit is zero; timeout is false;
process-group completion is true; and UTC intervals are nonoverlapping and in
command order.

The `cat-file` request is the ordered blob-ID population from `ls-files
--stage`; its stdout content is never persisted, while its SHA-256 is
independently reconstructed by streaming the full source archive. Every other
Git stdout is retained at the exact path `readiness/git/NN.stdout`. Path-list
outputs are strict NUL-terminated UTF-8, duplicate-free and bytewise sorted.
The all-other list equals the nontracked regular/symlink leaf population in the
retained `build`, `results`, and `Testing` snapshots; the ignored list is its
subset. Control files named `.gitignore` or `.gitattributes` are forbidden in
those roots. The other retained stdout hashes reconstruct the exact stage,
empty status/ancestor result, commit, and tree bytes. The detached verifier
requires the environment record and all 20 command summaries to match this
flow exactly and closes the entire `readiness/` namespace against missing,
aliased, and orphan payloads.

`unit_verification` has exact keys `argv`, `cwd`, `environment`, `started_utc`,
`finished_utc`, `exit_code`, `timed_out`, `process_group_complete`, `stdout`,
`stdout_sha256`, `stderr`, `stderr_sha256`, and `source_context_sha256`. It is
the descriptor-executed `/usr/bin/python3 -I -B` invocation of the held
verifier with `--verify-unit-anchor-prevalidated`, a private frozen copy of the
unit artifact, and the externally supplied manifest digest. Its cwd is `/tmp`;
its complete environment is exactly `PATH=/usr/bin:/bin`, `LANG=C`, and
`LC_ALL=C`; exit is zero; timeout is false; process-group completion is true;
and its source-context digest equals the barrier's. Its retained final stdout
line is the exact passing prevalidated-unit result for the held commit/tree.
The nested record is also projected without alteration as command zero in
`commands.jsonl`, using the exact `unit_verifier_v1` environment class, and the
detached actual verifier repeats the complete unit-anchor verification rather
than trusting that subprocess result alone.

The detached actual verifier may execute only the two workspace-local helper
modules `scripts/cp2/cp2_schema.py` and
`scripts/cp2/cp2_sequence_math.py`. It installs their path, Git mode, size, and
SHA-256 from the validated source context before loading either, opens each
through a real held `scripts/cp2` directory with `O_NOFOLLOW`, requires a
single-link regular file, reads/re-fstats/hashes the same descriptor, and
compiles those held bytes directly. An unbound name, path-reopening loader,
context mismatch, or cache/source mismatch is fatal. The explicitly marked
artifact-free self-test fixture is the only nonartifact binding mode.

This trusted-runner-local class does not claim malicious-runner attestation of
ephemeral pre-run `build`/`results` bytes.

## Common provenance and command records

Every artifact contains `provenance.json`, `commands.jsonl`, and
`SHA256SUMS`. `provenance.json` has exact top-level keys:

`schema_version`, `record_type`=`provenance`, `checkpoint`, `evidence_class`,
`distribution_status`, `eligible_for_cp2_seal`, `created_utc`, `branch`,
`source_commit`, `source_tree`, `source_archive` (relpath),
`source_archive_sha256`, `clean`, `cp1_authorization_commit`, `contracts`
(sorted `{path,sha256}` records), `entrypoints` (inventory-order
`{path,sha256,git_blob}` records), `readiness_barrier`, `unit_anchor`, `build`,
`readiness_barrier_sha256`, `runtime`, `configuration`, `inputs`, `environment`,
`host`, and `file_inventory`.

The required nested keys are:

- `unit_anchor`: `artifact`, `manifest_sha256`, `tested_commit`, `tested_tree`,
  `report_sha256`, and `verified`;
- `build`: `fresh_git_archive`, `workspace`, `commands_sha256`,
  `compile_commands` (relpath), `compile_commands_sha256`, `cmake_cache`
  (relpath), `cmake_cache_sha256`, and `strict_fp_verified`;
- `runtime`: exact keys `executable`, `executable_size`,
  `executable_sha256_before`, `executable_sha256_after`, `build_id_before`,
  `build_id_after`, and `runs`. Each run record has exact keys `run_id`,
  `sequence_index`, `mode`, `loader_map_before`, `loader_map_before_sha256`,
  `loader_map_after`, `loader_map_after_sha256`, `dso_records_before`, and
  `dso_records_after`. Each DSO record has exact keys `path`, `soname`, `size`,
  `sha256`, and `build_id`; arrays are bytewise-path sorted and identical
  before/after. Run records are in actual run order, join one-to-one to runtime
  contexts, and retain every CP2-C/D run (CP2-E remains blocked as above).
  Executable before/after bytes/build IDs are identical, and every run's
  path/SONAME/size/hash/build-ID DSO identity set is identical even though raw
  loader-map address bytes may differ;
- `configuration`: exact keys `static_files`, `static_bundle_payload`,
  `static_bundle_sha256`, `launch`, `resolved_parameters`, and
  `runtime_contexts`. Static records are exact `{path,size,sha256}` objects;
  launch is exact `{path,size,sha256}`; each resolved record has exact keys
  `run_id`, `prelaunch_raw_path`, `prelaunch_raw_sha256`, `runtime_raw_path`,
  `runtime_raw_sha256`, `canonical_path`, `canonical_sha256`,
  `normalized_path`, and `normalized_sha256`; each runtime-context record is
  exact `{run_id,path,size,sha256}` and records are in run order;
- `inputs`: sorted sequence records with `sequence_index`, `sequence_id`,
  `offset_seconds`, `bag_path`, `bag_size`, `bag_sha256_before`,
  `bag_sha256_after`, `ground_truth_path`, and `ground_truth_sha256`, with the
  final two nullable for CP2-C and mandatory for CP2-D;
- `environment`: exact key `classes`, a nonempty array of exact
  `{environment_id,variables,canonical_sha256}` objects sorted by
  `environment_id`. IDs are unique SAFE_ID strings. Each `variables` array is
  the complete environment passed to that command class, sorted strictly by
  UTF-8 name bytes, with exact unique `{name,value}` string objects. Names are
  restricted to `CC`, `CFLAGS`, `CMAKE_PREFIX_PATH`, `CP2_FORBID_BAG_ACCESS`,
  `CP2_POSTAUTH_PAIR_INDEX`, `CP2_SELF_TEST`, `CPATH`, `CXX`, `CXXFLAGS`,
  `GIT_ALLOW_PROTOCOL`, `GIT_ATTR_NOSYSTEM`, `GIT_CONFIG_NOSYSTEM`,
  `GIT_LITERAL_PATHSPECS`, `GIT_NO_LAZY_FETCH`, `GIT_NO_REPLACE_OBJECTS`,
  `GIT_OPTIONAL_LOCKS`, `GIT_PAGER`, `GIT_PROTOCOL_FROM_USER`,
  `GIT_TERMINAL_PROMPT`, `HOME`, `LANG`, `LC_ALL`,
  `LD_LIBRARY_PATH`, `LDFLAGS`, `LIBRARY_PATH`, `LOGNAME`, `OMP_NUM_THREADS`,
  `PATH`, `PKG_CONFIG_PATH`, `PYTHONDONTWRITEBYTECODE`, `PYTHONNOUSERSITE`,
  `PYTHONPATH`, `ROS_DISTRO`, `ROS_ETC_DIR`,
  `ROS_HOSTNAME`, `ROS_IP`, `ROS_MASTER_URI`, `ROS_PACKAGE_PATH`,
  `ROS_PYTHON_VERSION`, `ROS_ROOT`, `ROS_VERSION`, `SOURCE_DATE_EPOCH`,
  `TMPDIR`, and `USER`; and
- `host`: exact keys `hostname`, `os_release`, `kernel_release`, `architecture`,
  `cpu_model`, `logical_cpu_count`, `ros_distribution`, `compiler_version`,
  `cmake_version`, `catkin_version`, `eigen_version`, `opencv_version`,
  `boost_version`, `ceres_version`, `python_version`, and `evo_version`; a
  genuinely unavailable value is null.

`configuration.static_files` contains exactly the three EuRoC YAML records in
the literal order frozen by the static-bundle definition; `launch` is exactly
the fourth frozen record. `static_bundle_payload` is a relpath to the retained
canonical bundle bytes and `static_bundle_sha256` is both their digest and the
singular `config_sha256` joined by every CP2-C trace row. Each resolved record's
canonical/normalized hash covers the canonical binary parameter payload, not
the YAML text rendering. `prelaunch_raw_path` and `runtime_raw_path` name the
two separately retained dumps described below and their hashes cover those
exact bytes. For CP2-C, `normalized_path` and `normalized_sha256` are exactly
null. For CP2-D they are both nonnull and obey the normalization below. CP2-E
remains blocked until its separate execution profile freezes their
nullability; no timing evidence may use this draft to choose a value.

For each ROS run, the resolved-parameter population is the flattened map of
every parameter whose absolute name starts with `/cp2_vio/`, keyed by its full
absolute name; `/roslaunch`, global ROS state, parent namespaces, and namespace
container objects are excluded. The runner obtains a prelaunch map by exact
launch expansion without starting the node. The serial executable obtains the
runtime map after `VioManagerOptions::print_and_load` succeeds and immediately
before constructing any rosbag object or resolving/opening its bag path. The
two canonical typed payloads must be byte-identical and both raw dumps are
retained; otherwise the run stops before bag access. A trace row's
`resolved_parameters_sha256` is the SHA-256 of this full canonical runtime
payload, never the CP2-D normalized payload. CP2-D normalization starts only
from the already-equal full per-run maps and deletes the six explicitly
allowlisted leaf keys below.

The canonical full/normalized parameter payload is exactly the recursive
encoding frozen in the recorded-evidence contract: ASCII domain
`SchurVIO-CP2-ros-params-v1` plus one zero byte, followed by one top-level
`m` map containing the flattened absolute-name leaves. Every tag is its one
ASCII byte. Map/list counts and string lengths are big-endian u64; map entries
are bytewise-key sorted; `b` is followed by byte `00` or `01`; `i` by a
two's-complement big-endian i64; and `f` by exact big-endian IEEE-754 bits.
Null, nonfinite double, duplicate map key, embedded NUL, XML-RPC date/base64,
and every other type are rejected. The prelaunch and runtime canonical bytes
must be byte-identical.

Whenever the schema requires a typed JSON value, its exact object is
`{"type":"bool","value":Boolean}`,
`{"type":"int","value":i64}`, `{"type":"double","bits":HEX16}`,
`{"type":"string","value":string}`,
`{"type":"list","value":[typed values]}`, or
`{"type":"map","value":[{"name":string,"value":typed value},...]}`.
`HEX16` is the lowercase 16-digit binary64 bit pattern; map entries are
strictly bytewise-name sorted and lists retain order. These JSON objects map
one-to-one to the binary tags above.

`readiness_barrier` is a relpath to `readiness/barrier.json`, whose exact object
is defined above, and `readiness_barrier_sha256` hashes it. `file_inventory` is
a bytewise-path-sorted array covering every artifact regular file except
`cp2_report.json`, `provenance.json`, and `SHA256SUMS`, and has exact records
`{path,role,size,mode,sha256}`. Role is one
of `report`, `provenance`, `command`, `trace`, `payload`, `source`, `build`,
`configuration`, `readiness`, `log`, `trajectory`, or `evaluator`. The
manifest path set must equal this inventory plus `cp2_report.json` and
`provenance.json`; there are no unlisted/orphan files. This exclusion prevents
a report/provenance hash cycle.

Every inventory `size` and `mode` is u64, with mode equal to
`lstat().st_mode & 07777`; every path names a regular nonsymlink file with link
count one. Besides the fixed core filenames declared by the checkpoint
section, each inventory path must be referenced exactly once by a named
provenance/configuration/readiness field or by one `commands.jsonl`
stdout/stderr field. Command logs may not alias each other. A generic role label
alone never authorizes an additional file.

Each `commands.jsonl` object has exact keys `schema_version`, `record_type`
=`command`, `command_id` (contiguous from zero), `phase`, `sequence_index`,
`pair_index`, `run_index` (the last three nullable u64), `argv` (nonempty
string array), `cwd`, `environment_sha256`, `started_utc`, `finished_utc`,
`exit_code`, `timed_out`, `stdout` (relpath), `stdout_sha256`, `stderr`
(relpath), and `stderr_sha256`.
`phase` is exactly one of `readiness`, `source_archive`, `configure`, `build`,
`runtime_preflight`, `bag_identity`, `pair_index`, `ros_run`, `trajectory`,
`evaluation`, or `verification`.

Every top-level subprocess launched and recorded by the trusted runner starts
from an empty environment with exactly one retained environment class's
variables; no inherited or unlisted variable is present at that boundary.
Descendants created by the pinned `roslaunch` process may inherit or receive
ROS-injected variables and are instead covered by the retained resolved
parameters, executable/DSO maps, and launch bytes. A missing top-level name
means unset, null values are forbidden, and every name
and value must be valid UTF-8 without an embedded NUL before process creation.
A class's canonical bytes are the ASCII domain
`SchurVIO-CP2-command-environment-v1` plus one zero byte, a big-endian u64
variable count, then each sorted variable as a length-prefixed UTF-8 name and
length-prefixed UTF-8 value. `canonical_sha256` hashes those bytes. Every
command's `environment_sha256` equals exactly one retained class digest, and
every class is referenced by at least one command or readiness self-test; the
self-test class is referenced only by the five barrier records. The five readiness
self-tests use the exact five-variable class frozen by the readiness barrier;
their `CP2_SELF_TEST` and `CP2_FORBID_BAG_ACCESS` names are forbidden in every
other class.

The command population is exact: it contains every top-level subprocess the
trusted runner starts after the five readiness self-tests and before
`commands.jsonl` is sealed, including unit-anchor verification, source-archive,
configure/build, runtime, trajectory/evaluator, and CP2 offline-replay
subprocesses. Pure in-process operations have no command row. The five
self-tests are represented only by their readiness-barrier records and are
excluded from `commands.jsonl`, so their stdout/stderr files have one owning
reference. The final exact-byte artifact verification described below is also
excluded because it runs only after `commands.jsonl`, report, provenance, and
manifest are sealed.

Artifacts are created in a same-filesystem hidden partial directory, fsynced,
validated, and renamed without overwrite to
`results/staging/cp2/{recorded,sequence,timing}/SAFE_ID`. Finalized directories
are read-only, contain only regular files/directories, have no hardlinked
regular files, and remain `trusted_runner_local_staging_evidence`,
`internal_non_conveyable_staging`, and `eligible_for_cp2_seal=false`.
`SHA256SUMS` lists every regular file except itself exactly once, sorted by
bytewise relative path, as lowercase SHA-256, two spaces, relative path, and
newline. Its SHA-256 is supplied externally to the verifier.

Final verification runs against the fsynced, read-only hidden partial after all
artifact bytes and modes are sealed. Its stdout/stderr live in a fresh external
`/tmp` directory, are not artifact files or command rows, and are removed only
after the parsed final-line verification result passes. The same-filesystem
no-overwrite rename to the final staging name is an in-process operation and
occurs only after that pass; no artifact byte or mode changes between verifier
start and rename. This deliberate detached verification avoids a recursive
commands/report/provenance/manifest hash cycle.

## CP2-C recorded-parity artifact

The exact files are `cp2_report.json`, `provenance.json`,
`commands.jsonl`, `serial_pairs.jsonl`, `updates.jsonl`, `features.jsonl`,
`state_blocks.jsonl`, `covariance_blocks.jsonl`, all files named by the common
inventory,
`replay_report.json`,
`state_snapshot_payloads.bin`, `proposal_payloads.bin`,
`raw_system_payloads.bin`, and `SHA256SUMS`.

The three binary payload files retain the exact canonical bytes consumed by the
mathematical SHA-256 calculations. `state_snapshot_payloads.bin` starts with
the 32 bytes `SchurVIO-CP2-state-file-v1\n\0\0\0\0\0` and then zero or
more frames. Each frame is, in order, one phase byte (`0` pre-shadow, `1`
precommit, `2` cloned expected postcommit, `3` live postcommit), seven zero
reserved bytes, sequence index u64 big-endian, pair index u64 big-endian,
invocation ID u64 big-endian, payload length u64 big-endian, then exactly that
many canonical snapshot payload bytes. Phases 0/1 use domain
`SchurVIO-CP2-prior-snapshot-v1\0`; phases 2/3 use domain
`SchurVIO-CP2-postcommit-state-v1\0` followed by the same composite field
layout. Each shadow invocation with `raw_system_count>0` has phases 0/1; phase
1 is captured immediately before commit or, for a noncommitting invocation,
immediately before its terminal return. They are byte-identical. Each committed
counted invocation additionally has phases 2/3 and those are byte-identical.
No other frame combination is valid, and both equalities are independently
verified. Frames are in strict lexicographic order by
`(sequence_index,pair_index,invocation_id,phase)`. Trace offsets point to the
first canonical snapshot payload byte, not the frame header. There is no
inter-frame padding, footer, or trailing byte.

`proposal_payloads.bin` starts with the 32 bytes
`SchurVIO-CP2-proposal-file-v1\n\0\0` and then frames containing one role
byte (`0` nullspace baseline or `1` Schur candidate), seven zero reserved
bytes, sequence index, pair index, invocation ID and payload length as four u64
big-endian integers, then the payload. A proposal payload is domain
`SchurVIO-CP2-proposal-v1\0`, the `dx` vector, then the `P_plus` matrix under
the canonical matrix encoding. Every invocation has one baseline frame iff its
baseline preview is accepted and one candidate frame iff its candidate preview
is accepted. In particular, every committed counted update has exactly one
baseline frame. No other proposal frame is allowed.
Frames are in strict lexicographic order by
`(sequence_index,pair_index,invocation_id,role)`. Trace offsets point to the
first canonical proposal payload byte, not the frame header. There is no
inter-frame padding, footer, or trailing byte.

`raw_system_payloads.bin` starts with the 32 bytes
`SchurVIO-CP2-raw-file-v1\n\0\0\0\0\0\0\0` and then frames containing sequence
index, pair index, invocation ID, feature ordinal, feature ID and payload
length as six u64 big-endian integers, followed by the exact canonical raw
system payload bytes. Frame order is lexicographic by its five identity
integers. No padding, footer or trailing byte is allowed. Trace offsets point
to the first payload byte, not the frame header. An independent verifier
parses every frame, checks the domain prefix, recomputes its SHA-256, and
requires a one-to-one reference from the JSONL identities.

### Campaign record

`cp2_report.json` has exact keys:

`schema_version`, `record_type`=`recorded_parity_campaign`, `checkpoint`=`CP2-C`,
`status`, `evidence_class`, `distribution_status`, `eligible_for_cp2_seal`,
`created_utc`, `provenance_sha256`, `commands_sha256`, `serial_pairs_sha256`,
`updates_sha256`, `features_sha256`, `state_blocks_sha256`,
`covariance_blocks_sha256`, `state_snapshot_payloads_sha256`,
`proposal_payloads_sha256`,
`raw_system_payloads_sha256`, `replay_report_sha256`,
`proposal_derivation_passed`, `sequence_summaries`, `attempted_updates`,
`empty_input`, `all_rejected`, `empty_after_compression`,
`preflight_rejected`, `internal_failure`, `committing_updates`,
`minimum_committing_updates`, `raw_systems`, `nullspace_gate_attempts`,
`schur_gate_attempts`, `gate_union_denominator`, `gate_intersection`,
`gate_match_numerator`, `gate_ratio`, `row_denominator`, `row_match_numerator`,
`row_ratio`, `disagreement_counts`, `per_feature_statistics_passed`,
`state_blocks_expected`, `state_blocks_seen`, `covariance_blocks_expected`,
`covariance_blocks_seen`, `maximum_state_ratio`, `maximum_covariance_ratio`,
`candidate_missing_proposals`, `baseline_commit_mismatches`,
`shadow_write_totals`, `repair_fallback_totals`, `gate_passed`,
`math_passed`, and `passed`.

`status` is `passed` or `failed`. Each sequence summary has exact keys
`sequence_index`, `sequence_id`, `attempted_updates`, `committing_updates`,
`empty_input`, `all_rejected`, `empty_after_compression`,
`preflight_rejected`, `internal_failure`,
`first_pair_index`, `last_pair_index`, `first_camera_timestamp_ns`, and
`last_camera_timestamp_ns`; its six terminal counts reconcile exactly to
`attempted_updates`. `disagreement_counts` has exactly the seven keys
from the recorded-evidence contract. `shadow_write_totals` has exactly
`ekf_update_calls`, `type_update_calls`, `mean_writes`, `covariance_writes`,
and `feature_writes`. `repair_fallback_totals` has exactly `jitter`, `repair`,
`alternate_solve`, `clamp`, `regularization`, `silent_fallback`, and
`fallback`. Counts are u64. Ratios/maxima are nullable finite f64 and are null
only when their required population is zero, which itself fails CP2-C.
`replay_report_sha256` is sha256 and `proposal_derivation_passed` is Boolean.
The four first/last sequence fields are nullable u64, all null exactly when the
sequence has no updater invocation and otherwise nonnull with the exact first
and last values.

All campaign count fields and every nested count are u64. The two agreement
ratios equal the corresponding binary64 numerator divided by denominator;
their exact integer cross-products, not the rounded ratios, decide the gates.
`maximum_state_ratio` and `maximum_covariance_ratio` are the maxima over
available comparison rows and are null only if their respective available row
population is empty. `sequence_summaries` is in sequence-index order and the
aggregate terminal/count fields are the exact sums of its three records.
`minimum_committing_updates` is exactly u64 1000 and a passing artifact has
`committing_updates>=minimum_committing_updates`.
`passed` is true iff `status="passed"`, all three sequences completed, the
minimum committing count and both agreement gates pass, all mathematical and
block comparisons pass, every required row/payload/referential check passes,
aggregate and every sequence-summary `internal_failure` equal zero,
all candidate write and repair/fallback counts are zero, every exact live
commit comparison has zero mismatches, and all common provenance/readiness
requirements pass. `math_passed` is the conjunction of every update row's
`math_passed` (including baseline-gamma status), every required
feature-statistics pass, every state and covariance block pass, every
proposal-derivation check, and every exact live-commit check; `gate_passed` is
the conjunction of the two agreement cross-products. Failed evidence remains
serializable with these Booleans false.

### Serial-pair record

Each `serial_pairs.jsonl` object has exact keys:

`schema_version`, `record_type`=`serial_pair`, `sequence_index`, `sequence_id`,
`pair_index`, `anchor_filtered_index`, `anchor_camera_id`,
`cam0_filtered_index`, `cam1_filtered_index`, `cam0_record_time_ns`,
`cam1_record_time_ns`, `cam0_header_time_ns`, `cam1_header_time_ns`,
`camera_timestamp_ns`, `absolute_record_delta_ns`, `selected`,
`enqueue_entered`, `enqueue_returned`, `enqueue_status`,
`processing_entered`, `processing_returned`, `processing_status`, and
`updater_invocation_ids`.

Indices are contiguous and unique. `camera_timestamp_ns` equals
`cam0_header_time_ns`; `absolute_record_delta_ns` is strictly below 20,000,000.
The filtered-index pair and selection order must exactly match an independent
replay of the first-forward-message and used-message-deduplication algorithm.
Every row has `selected=true`. The camera enqueue and later estimator
processing are distinct because the serial camera callback only queues work;
the pair context travels by value with that queued measurement until the
single-threaded processing worker returns. `enqueue_status` is exactly
`queued`, `frequency_dropped`, `cam0_decode_failed`, `cam1_decode_failed`,
`not_entered`, `process_terminated`, or `trace_failure`. `processing_status` is
exactly `processed`, `not_queued`, `queued_unprocessed`, `process_terminated`,
or `trace_failure`. `processed` requires all four event Booleans true;
`not_queued` requires both processing Booleans false. Decode, trace, termination,
or queued-unprocessed status makes the campaign fail. A passing campaign
requires each selected row to have exactly one of two cross-status/event
transitions: `(queued,processed)` with all four event Booleans true, or
`(frequency_dropped,not_queued)` with both enqueue Booleans true and both
processing Booleans false. No other enqueue/processing-status pair is valid in
a passing artifact. `updater_invocation_ids` is empty unless processing status
is `processed`; a processed row may have an empty array, but every listed ID
must have been entered and returned within that processing interval.
`updater_invocation_ids` is a unique u64 array in call-entry order; every
CP2-C update joins exactly one processed pair and appears in exactly one such
array. All indices/timestamps/deltas are u64, flags are Boolean, and
`sequence_id` is the exact registry spelling.

### Update record

Each `updates.jsonl` object has exact keys:

`schema_version`, `record_type`=`updater_invocation`, `sequence_index`,
`sequence_id`, `pair_index`, `camera_timestamp_ns`, `invocation_id`,
`live_mode`, `shadow_mode`, `shadow_enabled`, `timing_evidence_eligible`,
`duration_ns`, `terminal_status`, `terminal_subreason`, `input_feature_count`,
`raw_system_count`, `prior_snapshot_sha256`, `precommit_snapshot_sha256`,
`prior_payload_offset`, `prior_payload_length`, `precommit_payload_offset`,
`precommit_payload_length`,
`expected_postcommit_snapshot_sha256`, `expected_postcommit_payload_offset`,
`expected_postcommit_payload_length`, `live_postcommit_snapshot_sha256`,
`live_postcommit_payload_offset`, `live_postcommit_payload_length`,
`baseline_proposal_sha256`, `baseline_proposal_payload_offset`,
`baseline_proposal_payload_length`, `candidate_proposal_sha256`,
`candidate_proposal_payload_offset`, `candidate_proposal_payload_length`,
`zero_write_snapshot_equal`, `baseline_accepted_ids`,
`baseline_accepted_set_sha256`, `baseline_accepted_sequence_sha256`,
`candidate_accepted_ids`, `candidate_accepted_set_sha256`,
`candidate_accepted_sequence_sha256`, `baseline_gamma_status`,
`baseline_gamma`, `candidate_gamma`,
`baseline_precompression_rows`, `baseline_compressed_rows`,
`candidate_precompression_rows`, `candidate_compressed_rows`,
`baseline_preview_status`, `baseline_preview_stage`, `candidate_outcome`,
`candidate_preview_status`, `candidate_preview_stage`, `candidate_proposal_available`,
`baseline_preview_counters`, `candidate_preview_counters`,
`baseline_commit_count`, `baseline_transaction_mean_commits`,
`baseline_covariance_commits`, `baseline_expected_type_update_calls`,
`baseline_verified_nominal_fields`, `baseline_nominal_mismatches`,
`baseline_covariance_mismatches`, `baseline_fej_mismatches`,
`candidate_ekf_update_calls`, `candidate_type_update_calls`,
`candidate_mean_writes`, `candidate_covariance_writes`,
`candidate_feature_writes`, `state_block_rows`, `covariance_block_rows`,
`all_block_rows_present`, `math_passed`, `config_sha256`, `bag_sha256`,
`pair_index_sha256`, and `resolved_parameters_sha256`.

`terminal_status` is exactly `empty_input`, `all_rejected`,
`empty_after_compression`, `preflight_rejected`, `committed_counted`, or
`internal_failure`. `terminal_subreason` is exactly `input_empty`,
`no_features_after_cleaning`, `no_features_after_triangulation`,
`no_raw_systems`, `all_baseline_features_rejected`,
`measurement_compression_empty`,
`baseline_preflight_rejected`, `invalid_live_mode`, `snapshot_mismatch`,
`trace_invariant_failure`, or `none`; `none` is required for
`committed_counted`. `live_mode` is `nullspace`; `shadow_mode` is `schur` when
enabled and null otherwise. Preview status is exactly `accepted`,
`invalid_input`, `nonfinite`, `factorization_failed`, or `negative_diagonal`;
preview stage is exactly the lower-case production `MSCKFUpdatePreviewStage`
name, and both are null when not reached. `candidate_outcome` is exactly
`not_enabled`, `not_reached`, `all_rejected`, `gamma_nonfinite`,
`empty_after_compression`, `preflight_rejected`, `proposal_available`, or
`internal_failure`. IDs are unique u64 arrays in processing order. SHA fields
are sha256 or null only when the corresponding list/snapshot does not exist.
Every committed record has one baseline commit, zero candidate writes, a pair
reference, `state_block_rows=B`, and `covariance_block_rows=B*B`.

`baseline_gamma_status` is exactly `not_reached`, `available`, or `nonfinite`.
It is `not_reached` exactly when no complete baseline accepted-list result
exists, including every `raw_system_count=0` terminal or an internal failure
before raw-system traversal completes. On completing that traversal, the
baseline diagnostic accumulator starts from exact positive zero and is
`available` when its ordered binary64 additions stay finite, including the
empty/all-rejected accepted list whose gamma is exact positive zero. It is
`nonfinite` when an accepted feature's addition first produces a nonfinite
sum; later diagnostic additions are skipped although baseline feature
processing continues. `baseline_gamma` is finite and nonnull exactly for
`available`, and null otherwise. A nonfinite baseline diagnostic makes
`math_passed=false` and fails the campaign, but it cannot alter feature
lifecycle, baseline compression/preflight, the live commit, counted status, or
terminal taxonomy. Candidate gamma failure remains represented by
`candidate_outcome=gamma_nonfinite` and likewise never changes the baseline
terminal mapping. At its first nonfinite sum, candidate gamma remains null and
later diagnostic additions are skipped, but Schur reduction, gate/statistics,
and accepted-ID traversal continue to completion; only candidate global
compression/preview is unavailable. Every candidate gamma failure also makes
the update `math_passed=false` and fails the campaign, whether or not the
baseline terminal is counted.

The terminal/subreason mapping is exact: `empty_input/input_empty`;
`all_rejected` with one of `no_features_after_cleaning`,
`no_features_after_triangulation`, `no_raw_systems`, or
`all_baseline_features_rejected`;
`empty_after_compression/measurement_compression_empty`;
`preflight_rejected/baseline_preflight_rejected`;
`committed_counted/none`; or `internal_failure` with one of
`invalid_live_mode`, `snapshot_mismatch`, or `trace_invariant_failure`.
Candidate failure and baseline-gamma diagnostic failure never change this
mapping.
Baseline preview status/stage is nonnull exactly when baseline preflight is
reached; accepted status is required before any baseline proposal exists.

All nonnull identities, counts, dimensions, durations, and payload
offsets/lengths in an update are u64; all write/commit/mismatch fields are u64 counts. Mode,
status, stage, and outcome fields are strings from their declared enums and
flags are Boolean. The four configuration/input hashes are nonnull sha256 on
every row. Accepted-ID fields are unique u64 arrays in processing order. Their
four digests are nonnull and reconstructible even for an empty list. Baseline
gamma obeys `baseline_gamma_status` above. Candidate gamma is finite f64,
including exact positive zero for an empty accepted list, once that path has a
terminal accepted-list result; it is null only for an explicitly
nonfinite/internal path. Precompression and
compressed row counts are u64 whenever their stage was reached and otherwise
null. Snapshot hashes and
offset/length triples are nonnull exactly for the phase combinations required
by the binary-payload rules above. Proposal hash/offset/length triples are
nonnull exactly when the matching preview is accepted. `duration_ns` is always
nonnull; timing eligibility is always false for this shadow-enabled campaign.
Every nullable triple is wholly null or wholly nonnull. `math_passed` is false
on any `internal_failure` terminal, nonfinite baseline or candidate gamma
diagnostic, missing required candidate
proposal, comparison failure, nonzero write, nonzero fallback/repair, snapshot
mismatch, or live-commit mismatch.
Prior/precommit snapshot triples are nonnull iff `raw_system_count>0`;
expected/live postcommit triples are nonnull iff the terminal status is
`committed_counted`. `zero_write_snapshot_equal` is true for a zero-raw-system
invocation and otherwise is the independently checked phase-0/1 byte equality.
Commit and expected/verified-field counts are zero on every noncommitting
terminal. State/covariance block row counts are respectively zero/zero on a
noncommitting terminal and `B`/`B*B` on a commit; `all_block_rows_present` is
true iff that terminal-dependent exact population is present.

### Feature record

Each `features.jsonl` object has exact keys:

`schema_version`, `record_type`=`feature_comparison`, `sequence_index`,
`pair_index`, `camera_timestamp_ns`, `invocation_id`, `feature_ordinal`,
`feature_id`, `pass_index`, `raw_rows`, `raw_system_sha256`, `jacobian_layout`,
`raw_payload_offset`, `raw_payload_length`,
`prior_snapshot_sha256`, `config_sha256`, `bag_sha256`,
`pair_index_sha256`, `resolved_parameters_sha256`,
`baseline_accepted_set_sha256`, `baseline_accepted_sequence_sha256`,
`candidate_accepted_set_sha256`, `candidate_accepted_sequence_sha256`,
`baseline_retained_gamma`, `candidate_retained_gamma`,
`nullspace_reduction_status`, `nullspace_reduction_stage`,
`nullspace_reduced_rows`, `nullspace_raw_lambda_symmetry_error_inf`,
`nullspace_gamma`, `nullspace_nis`,
`nullspace_threshold`, `nullspace_gate_stage`, `nullspace_decision`,
`schur_reduction_status`, `schur_reduction_stage`, `schur_reduced_rows`,
`schur_singular_values_available`, `schur_singular_values`,
`schur_ratio_available`, `schur_ratio`,
`schur_raw_lambda_symmetry_error_inf`, `schur_gamma`, `schur_nis`,
`schur_threshold`, `schur_gate_stage`, `schur_decision`,
`statistics_comparison_required`, `lambda_comparison_available`,
`eta_comparison_available`, `gamma_comparison_available`,
`lambda_comparison_status`, `eta_comparison_status`,
`gamma_comparison_status`,
`lambda_reference_norm`, `lambda_error`, `lambda_tolerance`, `lambda_ratio`,
`lambda_pass`, `eta_reference_norm`, `eta_error`, `eta_tolerance`, `eta_ratio`,
`eta_pass`, `gamma_reference_norm`, `gamma_error`, `gamma_tolerance`,
`gamma_ratio`, `gamma_pass`, `agreement_class`, `raw_row_match_weight`,
`nullspace_reducer_counters`, and `schur_reducer_counters`.

`jacobian_layout` is an ordered array of exact `{covariance_id,size}` objects.
`pass_index` is the integer one on every feature record; every other value is
invalid.
Unavailable numeric diagnostics are null and their availability/decision flags
are false/null. All identity/count/dimension/offset/length/weight fields are
u64; availability flags are Boolean; repair/fallback fields are u64; decision
and statistics-pass fields are nullable Boolean; hashes are sha256;
`jacobian_layout` sizes and covariance IDs are u64. Gate stages and agreement
classes are exactly those frozen in
the recorded-evidence contract. Nullspace reduction status is `accepted`,
`invalid_dimensions`, or `nonfinite`, and its stage is `input_dimensions`,
`givens_output`, `statistics`, or `accepted`. Schur reduction status/stage is
the lower-case production enum name. `schur_singular_values` is null when its
availability flag is false and otherwise an exact three-f64 array. Each
decision is null unless its gate stage is `decision`, then Boolean.
`statistics_comparison_required` is true iff both reductions are mode-valid.
When it is false, all three comparison-availability flags are false and all
three statuses are `not_required`, and all fifteen numeric/pass fields are
null. A comparison status is exactly `not_required`, `available`,
`reference_unavailable`, `candidate_unavailable`, `reference_norm_nonfinite`,
`error_nonfinite`, `tolerance_nonfinite`, or `ratio_nonfinite`. When comparison
is required, each comparison is handled independently in this first-failure
precedence: reference unavailable, candidate unavailable, evaluate the
reference norm, evaluate the candidate-minus-reference error, evaluate the
tolerance multiplication then addition, evaluate the ratio division, then
`available`. No later operation is evaluated after a failure. An available
comparison has four nonnull numeric fields and a Boolean pass; any other status
has all four numeric fields null,
availability=false and pass=false, making `math_passed` and the campaign `passed`
false while preserving valid failed evidence. Every nonnull norm, error,
tolerance and ratio is a
finite nonnegative f64, each tolerance is reconstructed as
`1e-10+1e-8*reference_norm`, and each ratio is `error/tolerance` (including
zero when error is zero). Pass is exactly the direct `error<=tolerance`
comparison; the stored ratio is diagnostic and never the decision input.
Each raw-Lambda symmetry error is a finite nonnegative f64 exactly when its
mode-valid statistics were formed and is otherwise null. The verifier
recomputes both before accepting the later symmetrized Lambda values.
`raw_row_match_weight` equals `raw_rows` exactly for `both_match_accept` and
`both_match_reject`, and zero for all five other classes. The denominator
contribution is independently reconstructed as `raw_rows` for every class
except `neither_decision`, whose contribution is zero.

Each of the two update preview-counter objects and two feature reducer-counter
objects has exact u64 keys `jitter`, `repair`, `alternate_solve`, `clamp`,
`regularization`, `silent_fallback`, and `fallback`. A source that has no
corresponding production field emits exact zero; counters may not be omitted or
merged. `repair_fallback_totals` is reconstructed key-by-key as the sum of both
feature reducer objects over every feature row plus both preview objects over
every update row. Every source counter and total is zero in a passing artifact.

### Block records

Each `state_blocks.jsonl` object has exact keys `schema_version`, `record_type`
=`state_block_comparison`, the update identity quartet, `block_index`,
`block_kind`, `block_identity`, `covariance_id`, `size`, `candidate_available`,
`comparison_available`, `comparison_status`, `reference_norm`, `error`,
`tolerance`, `ratio`, `prior_snapshot_sha256`,
`config_sha256`, `bag_sha256`, `pair_index_sha256`,
`resolved_parameters_sha256`, `baseline_accepted_set_sha256`,
`baseline_accepted_sequence_sha256`, `candidate_accepted_set_sha256`,
`candidate_accepted_sequence_sha256`, `baseline_retained_gamma`,
`candidate_retained_gamma`, and `passed`.

Each `covariance_blocks.jsonl` object has the same update identity plus exact
keys `schema_version`, `record_type`=`covariance_block_comparison`,
`row_block_index`, `column_block_index`, `row_covariance_id`,
`column_covariance_id`, `row_size`, `column_size`, `candidate_available`,
`comparison_available`, `comparison_status`, `reference_norm`, `error`,
`tolerance`, `ratio`, `prior_snapshot_sha256`,
`config_sha256`, `bag_sha256`, `pair_index_sha256`,
`resolved_parameters_sha256`, `baseline_accepted_set_sha256`,
`baseline_accepted_sequence_sha256`, `candidate_accepted_set_sha256`,
`candidate_accepted_sequence_sha256`, `baseline_retained_gamma`,
`candidate_retained_gamma`, and `passed`.

`block_identity` is the canonical typed metadata retained by the prior schema.
Its exact JSON shape is `{kind}` for each IMU block;
`{kind,timestamp_bits}` for clone blocks, where `timestamp_bits` is 16
lowercase hexadecimal IEEE-754 bits; and
`{kind,feature_id,representation,anchor_camera_id,anchor_timestamp_bits}` for a
SLAM landmark. Key inventory is exact, `kind` matches `block_kind`, and
representation is one exact `LandmarkRepresentation::as_string` value.
The comparison status is exactly `available`, `candidate_unavailable`,
`reference_norm_nonfinite`, `error_nonfinite`, `tolerance_nonfinite`, or
`ratio_nonfinite`. Evaluate one row in this precedence order: reject a missing
candidate; evaluate the baseline-reference Eigen binary64 Euclidean norm for
a state vector or Frobenius norm for a covariance matrix; form the
candidate-minus-reference coefficient differences and evaluate the same kind
of Eigen norm; evaluate the binary64 multiplication
`1e-6*reference_norm` followed by the addition `1e-8+product`; then evaluate
`error/tolerance` with one binary64 division. Evidence translation units use
strict no-fast-math and no floating-point contraction. A nonfinite difference
coefficient or nonfinite error norm has status `error_nonfinite`. At the first
nonfinite/unavailable step, later operations are not evaluated.

Only `available` has `comparison_available=true` and four nonnull numeric
fields; they are finite nonnegative f64, the tolerance has the exact operation
order above, and `passed` is exactly `error<=tolerance`; the rounded ratio is
never used as an equivalent decision. Every other status has
`comparison_available=false`, all four
numeric fields null, and `passed=false`; `candidate_available` independently
states whether the global candidate proposal existed. Thus finite proposal
coefficients whose subtraction, Eigen norm, tolerance, or ratio overflows are
serializable failed evidence rather than an omitted row.
For each counted update, block indices are contiguous `0..B-1`, state rows
contain every index exactly once, and covariance rows contain every ordered
Cartesian pair exactly once.

The update identity quartet is exactly `sequence_index`, `pair_index`,
`camera_timestamp_ns`, and `invocation_id`. Block indices, covariance IDs and
sizes are u64; `candidate_available` and `passed` are Boolean. All repeated
hash/gamma fields equal their joined update record. `comparison_available` is
Boolean and `comparison_status` obeys the exact state machine above. Candidate digest/gamma
fields remain the update record's actual values even when its global proposal
is unavailable. The five provenance/input hashes are nonnull. Candidate
availability is uniform across all block rows for one update.

### Offline raw-to-proposal replay proof

`replay_report.json` has exact keys `schema_version`, `record_type`
=`cp2_offline_replay`, `checkpoint`=`CP2-C`, `source_commit`, `source_tree`,
`executable_sha256`, `executable_build_id`, `strict_fp_verified`,
`bag_provider_calls`, `input_sha256`, `resolved_parameters`,
`replayed_invocations`, `replayed_raw_systems`,
`baseline_expected_proposals`, `candidate_expected_proposals`,
`baseline_exact_proposal_matches`, `candidate_exact_proposal_matches`,
`failure_counts`, and `passed`. `input_sha256` has exact keys `serial_pairs`,
`updates`, `features`, `state_blocks`, `covariance_blocks`,
`state_snapshot_payloads`, `proposal_payloads`, and `raw_system_payloads` and
hashes those final retained bytes. `resolved_parameters` is a
sequence-index-ordered array of exact `{sequence_index,sha256}` objects.
`failure_counts` has exact u64 keys `layout`, `reduction`, `statistics`, `gate`,
`accepted_sequence`, `gamma`, `stack`, `compression`, `preview_status`,
`proposal_presence`, `proposal_bytes`, `commit_oracle`, and `block_metrics`. All replay/count
fields and `bag_provider_calls` are u64, the latter is zero; identity fields
match provenance, `strict_fp_verified` is true, and `passed` is true exactly
when every failure count is zero, `replayed_invocations` equals the independently
counted nonzero-`raw_system_count` update population and is nonzero,
`replayed_raw_systems` equals both the summed update raw-system counts and the
one-to-one feature/raw-frame population and is nonzero, each expected-proposal
count equals the proposal presence independently derived by replay, and both
exact-match counts equal their corresponding expected-proposal counts. A
zero-count/no-op or skipped invocation cannot pass.

The executable SHA-256/build ID must equal the before/after runtime executable
identity and the source commit/tree must equal the artifact and unit anchor.
For every invocation with `raw_system_count>0`, the offline kernel decodes its
required phase-0 prior snapshot and every joined raw-system frame. A zero-raw
invocation is not counted as replayed and is instead structurally required to
have no phase-0/raw frame and to obey its terminal/payload rules. For the replay
population, the kernel requires each layout's ordered size sum to
equal raw `H_x.cols()`, every `(covariance ID,size)` to resolve uniquely to an
active top-level prior block with that exact size, and all resolved covariance
ranges to be nonoverlapping and in bounds. It rejects a duplicate/conflicting
layout entry or a raw/prior identity disconnect.

From fresh owning copies, the hash/build-ID-anchored strict-FP kernel then does
all of the following without consulting a retained decision, accepted list,
stack, or proposal:

1. call the unchanged production Givens nullspace projection and
   `SchurUpdate::Reduce` on the same raw `H_x,H_f,r,sigma_px`;
2. recompute the exact nullspace/Schur statistics and the shared gate helper
   from the phase-0 prior marginal and ordered layout;
3. traverse feature ordinals once in processing order, independently derive
   both lifecycle accepted sequences/digests and ordered gamma states, and
   preserve the contracted continue-after-gamma-overflow behavior;
4. for each path independently, assemble accepted reduced rows using global
   Jacobian columns in first-seen `(covariance ID,size)` order, call the same
   production `UpdaterHelper::measurement_compress_inplace`, construct
   `sigma_px_sq*Identity` with the production expression, and call the shared
   value-only `UpdaterMSCKFPreview::ComputeFromSnapshot` on phase-0 covariance;
   and
5. encode every accepted replay `dx,P_plus` with the canonical proposal domain.

For every replayed committed baseline, the kernel then instantiates detached
active top-level production `Type` objects from phase 0, retains landmark
representation/feature/camera/anchor metadata separately, and applies each
covariance-ID-ordered segment of the replayed baseline `dx` exactly once with
the production `Type::update`. It constructs the complete expected postcommit
composite from those nominal values, unchanged phase-0 FEJ/fixed-calibration/
camera-cache/timestamp fields, and the replayed `P_plus`. Those canonical bytes
must be byte-identical to both phase-2 expected and phase-3 live snapshots.
Every noncommitting invocation must instead lack both phases. This commit
oracle is derived from phase 0 and the replayed proposal; phase-2/phase-3
equality alone is insufficient.

The replay requires exact status/stage, reduced-row count, singular-value
availability/value bits, singular ratio, raw-symmetry error, gamma, NIS,
threshold, official decision, agreement class, accepted IDs/order/digests,
gamma status/value bits, precompression/compressed row counts, candidate
outcome, preview status/stage/counters, and proposal presence to equal the
trace. Every expected canonical proposal payload must be byte-identical to its
role/identity-matched retained payload; numeric tolerance is forbidden for
this derivation check. Exact absence is required when replay has no proposal.
Only after those checks pass does the replay recompute all state/covariance
block statuses, diagnostics, and pass flags from the replayed proposals and
phase-0 partition.

The runner performs this replay once before sealing and retains its deterministic
report. Final detached `verify_report.py --verify-recorded` reruns the same
offline mode into `/tmp`, requires the new report byte-identical to
`replay_report.json`, then applies its independent structural/arithmetic
checks. `cp2_report.replay_report_sha256` is the ordinary SHA-256 of the
retained report, `proposal_derivation_passed` equals its `passed`, and
`math_passed`/`passed` cannot be true unless it is true.

### CP2-C referential and arithmetic checks

Sequence, pair, camera timestamp, invocation, feature and block identities must
join exactly across all files. Invocation IDs are contiguous within one
updater instance and may not duplicate. Every update has exactly
`raw_system_count` feature records; input features that fail cleaning or
triangulation never acquire a raw-system row. The six terminal counts sum
exactly to attempted updates.
Only `committed_counted` contributes to the thousand-record minimum. Every
invocation with at least one member of the gate-decision union contributes to
the official gate and raw-row agreement integers. Feature classes reproduce
all numerators and denominators exactly; integer cross-products enforce 0.999. Every statistics
and block pass flag is recomputed from retained primitives. A candidate-missing
counted update has all required unavailable block rows and fails the campaign.
All zero counters and exact live-commit comparisons are recomputed. Summary
values are never trusted without raw-row reconstruction.

Final JSONL row order is strict: serial rows by
`(sequence_index,pair_index)`; updates by
`(sequence_index,pair_index,camera_timestamp_ns,invocation_id)`; features by
that update tuple then `feature_ordinal`; state blocks by that update tuple then
`block_index`; and covariance blocks by that update tuple then
`(row_block_index,column_block_index)`. Each ordering key is unique in its
file. The per-sequence staging concatenation/split must produce exactly these
orders.

## CP2-D sequence-pair artifact

One invocation produces one read-only artifact containing exactly
`cp2_report.json`, `provenance.json`, `commands.jsonl`, `pair_index.jsonl`,
`nullspace_callbacks.jsonl`, `schur_callbacks.jsonl`,
`nullspace_trajectory.jsonl`, `schur_trajectory.jsonl`,
`parameters/nullspace_prelaunch_raw.yaml`,
`parameters/nullspace_runtime_raw.yaml`, `parameters/nullspace_canonical.bin`,
`parameters/nullspace_normalized.bin`,
`parameters/schur_prelaunch_raw.yaml`, `parameters/schur_runtime_raw.yaml`,
`parameters/schur_canonical.bin`, `parameters/schur_normalized.bin`,
`nullspace_state.txt`, `nullspace_deviation.txt`,
`nullspace_openvins_timing.csv`, `schur_state.txt`, `schur_deviation.txt`,
`schur_openvins_timing.csv`, `nullspace_raw.tum`,
`schur_raw.tum`, `ground_truth_shared.tum`,
`nullspace_shared_aligned.tum`, `schur_shared_aligned.tum`,
`shared_population.bin`, `shared_timestamps.bin`, evaluator logs, and
`SHA256SUMS`. `cp2_report.json`
has exact keys:

`schema_version`, `record_type`=`sequence_pair`, `checkpoint`=`CP2-D`,
`status`, `sequence_index`, `sequence_id`, `offset_seconds`, `provenance_sha256`,
`pair_index_sha256`, `valid_pair_count`, `runs`, `normalized_parameter_diff`,
`shared_timestamp_count`, `shared_timestamp_sha256`, `shared_population_sha256`, `baseline_alignment`,
`position_p95_m`, `orientation_p95_deg`, `ate_nullspace_m`, `ate_schur_m`,
`relative_ate_difference`, `coverage_passed`, `trajectory_passed`, and `passed`.

`runs` has exactly two records in `nullspace`, `schur` order, each with exact
keys `run_index`, `mode`, `executable_sha256`, `loader_map_sha256`,
`resolved_parameters_sha256`, `callback_trace_sha256`, `trajectory_sha256`,
`processed_unique_pairs`, `processing_fraction`, `first_selected_timestamp_ns`,
`last_selected_timestamp_ns`, `first_processed_timestamp_ns`,
`last_processed_timestamp_ns`, `selected_duration_ns`, `processed_duration_ns`,
`time_coverage`, `completed`, and `exit_code`. The normalized resolved maps are
byte-identical after deleting exactly these six absolute keys and no others:
`/cp2_vio/up_msckf_landmark_elimination`, `/cp2_vio/filepath_est`,
`/cp2_vio/filepath_std`, `/cp2_vio/record_timing_filepath`,
`/cp2_vio/cp2_trace_directory`, and `/cp2_vio/cp2_context_path`. The raw maps,
deleted `{key,typed_value}` records, normalized canonical payloads, and hashes
are retained. Missing an allowlisted key or finding any other difference fails.
The four first/last timestamp fields in each run are cam0 rosbag record-time
nanoseconds, not header stamps or rounded state timestamps.

`normalized_parameter_diff` is an array in the six-key order above. Each record
has exact keys `key`, `nullspace_typed_value`, and `schur_typed_value`, using the
same recursive typed JSON value grammar as the canonical parameter encoding.
The mode values are respectively `nullspace` and `schur`; the five path values
are distinct absolute nonsymlink paths inside their run's hidden partial
directory. No other normalized difference is permitted.

Each `pair_index.jsonl` row has exact keys `schema_version`, `record_type`
=`pair_index`, `sequence_index`, `sequence_id`, `pair_index`,
`anchor_filtered_index`, `anchor_camera_id`, `cam0_filtered_index`,
`cam1_filtered_index`, `cam0_record_time_ns`, `cam1_record_time_ns`,
`cam0_header_time_ns`, `cam1_header_time_ns`, and
`absolute_record_delta_ns`. Pair indices are contiguous from zero and rows are
exactly the independently selected valid pairs; the delta is strictly below
20,000,000 ns.

Each mode callback JSONL row has exact keys `schema_version`, `record_type`
=`serial_callback`, `sequence_index`, `sequence_id`, `mode`, `callback_index`,
`pair_index`, `anchor_filtered_index`, `cam0_filtered_index`,
`cam1_filtered_index`, `cam0_record_time_ns`, `cam1_record_time_ns`,
`cam0_header_time_ns`, `cam1_header_time_ns`, `camera_timestamp_ns`,
`enqueue_entered`, `enqueue_returned`, `enqueue_status`, `processing_entered`,
`processing_returned`, `processing_status`, `state_row_emitted`, and
`trajectory_index`.
`callback_index` is contiguous in actual callback order; every referenced pair
exists, is unique within a mode, and all source fields equal the pair-index
row. `camera_timestamp_ns` equals cam0 header time. The two status enums and
four Boolean events follow the exact common rules above. Processing fraction is
the number of unique `processed` rows divided by the full pair-index row count.
Both counts must be positive. Time coverage is
`(last_processed_cam0_record_ns-first_processed_cam0_record_ns) /
(last_selected_cam0_record_ns-first_selected_cam0_record_ns)` using the first
and last pair-index rows and first/last `processed` rows; both durations must
be strictly positive. Each mode independently requires both ratios at least
0.995.

`trajectory_index` is null iff `state_row_emitted=false`; otherwise it is u64.
Each mode trajectory JSONL row has exact keys `schema_version`, `record_type`
=`trajectory_pose`, `sequence_index`, `sequence_id`, `mode`,
`trajectory_index`, `callback_index`, `pair_index`, `camera_timestamp_ns`,
`position_G` (three f64), and `quaternion_ItoG_xyzw` (four f64).
Trajectory indices are contiguous from zero, timestamps are strictly
increasing and unique, and each row joins one `processed` callback whose
`trajectory_index` points back to it. No callback has more than one trajectory
row. The position is the live IMU position in the estimator global frame after
that callback. OpenVINS stores the JPL quaternion for `R_GtoI`; its four stored
`xyzw` coefficients are, by the frozen JPL/Hamilton convention, copied without
component reordering as the Hamilton quaternion of the inverse rotation
`R_ItoG`. This convention is protected by a nonidentity rotation self-test.

Each TUM file has one exact header
`# timestamp tx ty tz qx qy qz qw\n`, then eight finite fields separated by one
ASCII space and one newline. Estimator timestamps are formatted from integer
header nanoseconds as `seconds.nanoseconds` with exactly nine fractional
digits; each raw estimator TUM is the one-to-one trajectory-index-order
projection of its mode trajectory JSONL and pose fields use lowercase C-locale
`%.17g`. Ground-truth decimal
timestamps are parsed exactly, must have at most nine fractional digits, and
are converted to integer nanoseconds without binary64 rounding. Quaternions
must have norm in `[1-1e-10,1+1e-10]` and rotations are formed only after
normalization.

Mode output population is the exact intersection of unique
`camera_timestamp_ns` values from the two completed callback/trajectory traces.
In increasing
estimator time, each timestamp is associated to the ground-truth row with
minimum absolute integer-nanosecond difference at most 10,000,000; ties select
the lower ground-truth row index and ground-truth reuse is allowed, matching
evo 1.31.1. `shared_population.bin` starts with domain bytes
`SchurVIO-CP2-shared-population-v1\0`, then a u64 count, then for every row:
estimator timestamp u64, ground-truth timestamp u64, nullspace position and
quaternion, Schur position and quaternion, and ground-truth position and
quaternion, with every pose component encoded as canonical binary64. Its
SHA-256 is `shared_population_sha256`. `shared_timestamp_sha256` hashes domain
`SchurVIO-CP2-shared-timestamps-v1\0`, the same count, and only the increasing
estimator timestamps as u64. That exact second payload is retained as
`shared_timestamps.bin`. The two raw TUM files are deterministic projections of
their full trajectory JSONL; the three `*_shared*` TUM files are deterministic
projections of the shared payload and the one common alignment.

`baseline_alignment` has exact keys `source`=`nullspace_to_ground_truth`,
`shared_population_sha256`, `rotation_row_major` (nine f64), `translation`
(three f64), `quaternion_xyzw` (four f64), `source_singular_values` (three
f64), `source_rank_threshold`, `determinant`,
`orthogonality_error_frobenius`, and `applied_identically_to_both_modes`=true.
One common association population and transform are used; independent mode
alignment is forbidden. The verifier independently recomputes pair selection,
coverage, association, transform application, linear p95 values and ATE inputs.

On the exact shared timestamp population, let nullspace positions be `x_k` and
ground-truth positions be `y_k`. With binary64 arithmetic, compute their
centroids and `C=(1/N) sum_k (y_k-y_bar)(x_k-x_bar)^T`. For a full SVD
`C=U Sigma V^T`, set
`D=diag(1,1,sign(det(U V^T)))`, `R=U D V^T`, and
`t=y_bar-R x_bar`; a zero determinant sign, nonfinite value, or failed SVD is
invalid. Apply the identical retained bytes `(R,t)` to both modes as
`p'_k=R p_k+t`. OpenVINS stores `R_GtoI`, so the aligned state rotation is
`R_G'toI=R_GtoI R^T`; the aligned TUM quaternion encodes its transpose
`R_ItoG'`. Position mode difference is `||p'_S-p'_N||_2`; orientation
difference is
`acos(clamp((trace(R_G'toI,N^T R_G'toI,S)-1)/2,-1,1))` in degrees. Linear p95
uses the same interpolation definition as CP2-E.
The retained rotation must satisfy `|det(R)-1|<=1e-10` and
`||R^T R-I||_F<=1e-10`; these are validation tolerances, not repairs.

ATE is the translation RMSE on these already associated and commonly aligned
TUM rows. The retained evaluator command is exactly
`evo_ape tum GT_SHARED.tum MODE_SHARED_ALIGNED.tum -r trans_part
--t_max_diff 0.01`; `-a`, `--align`, scale correction, and a second association
population are forbidden. The verifier recomputes RMSE directly and requires
the parsed evo value to agree within `1e-12 + 1e-10*|reference|`.
`ate_nullspace_m` must be finite and strictly positive before evaluating the
frozen relative-ATE formula; a zero baseline ATE is invalid rather than a
special-case pass. Before alignment, require `N>=3` and compute the singular
values `a_1>=a_2>=a_3>=0` of the 3-by-N centered nullspace position matrix in
increasing timestamp column order. Require a complete finite spectrum,
`a_1>std::numeric_limits<double>::min()`, and
`a_2>max(N,3)*epsilon*a_1`; equality fails. The last product is the retained
`source_rank_threshold`. Thus a collinear or coincident population cannot
obtain an arbitrary alignment rotation.

Across final CP2-D assembly, sequence artifacts must have exact indices
`0,1,2` in argument order with no duplicate or missing sequence. The
`--verify-sequence-set` mode independently verifies all three manifests,
requires identical source/runtime/config/launch identities, and emits one
schema-1 aggregate `cp2_report.json` to stdout with exact keys
`schema_version`, `record_type`=`sequence_set`, `checkpoint`=`CP2-D`,
`sequence_indices`, `sequence_manifest_sha256`, `all_individually_passed`,
`cross_sequence_identity_equal`, and `passed`.

## CP2-E timing preregistration (not yet an evidence schema)

Actual timing evidence remains blocked until a separately committed
`project/cp2_timing_profile.yaml` freezes sequence/offset/input hash, CPU IDs,
affinity, governor, driver, min/max/current frequencies, boost/turbo state and
sampling commands. The runner validates but never changes these controls.
Without that profile, or under `ondemand`, it may produce only a clearly marked
functional diagnostic outside timing evidence and must not open a bag through
the actual evidence CLI.

A future eligible artifact will contain `cp2_report.json`, `provenance.json`,
`commands.jsonl`, `timing_samples.jsonl`, six run traces, clock snapshots, and
`SHA256SUMS`. The profile commit must replace this preregistration with exact
schemas for the profile, report, runs, pair results, clock snapshots, common
payloads, and every file before actual mode is enabled. At minimum, the report
will include:

`schema_version`, `record_type`=`timing_campaign`, `checkpoint`=`CP2-E`,
`status`, `profile_sha256`, `provenance_sha256`, `sequence_index`,
`sequence_id`, `bag_begin_record_time_ns`, `frozen_offset_ns`,
`timing_samples_sha256`, `pair_order`, `runs`,
`pair_results`, `median_of_three_median_ratios`,
`median_of_three_p95_ratios`, `every_pair_passed`, and `passed`.

`pair_order` is exactly `[["nullspace","schur"],["schur","nullspace"],
["nullspace","schur"]]`. Six `runs` have contiguous pair/run indices and
retain identical executable/DSO/config/input/pair-index identities, exact
affinity and pre/post clock snapshots. Each `timing_samples.jsonl` row has
exact keys `schema_version`, `record_type`=`updater_timing`,
`timing_pair_index`, `run_index`, `mode`, `serial_pair_index`,
`cam0_record_time_ns`, `camera_timestamp_ns`, `invocation_id`,
`terminal_status`, `nonempty`, `preflight_accepted`, `committed`, `primary`,
and `duration_ns`.

For each pair, the verifier forms the exact intersection of unique primary
committing camera timestamps whose joined `cam0_record_time_ns` is at or after
`bag_begin_record_time_ns + frozen_offset_ns + 60,000,000,000`. Bag begin is
the exact record-time nanoseconds returned by the full rosbag view and is
retained in the report; the frozen offsets 40, 5 and 0 seconds are converted to
integer nanoseconds exactly. Record time selects warm-up eligibility, while
the exact cam0 header timestamp keys the cross-mode intersection. It recomputes
linear quantiles from sorted integer nanoseconds with
`h=(n-1)q`, `lo=floor(h)`, `hi=ceil(h)`, and
`x_lo+(h-lo)*(x_hi-x_lo)` for `q=.5,.95`. Each pair record retains common count
and hash, both mode quantiles, candidate/baseline ratios and pass flags. Every
median ratio is at most 1.10 and every p95 ratio at most 1.15.

For timing pair `j`, its common-population hash is SHA-256 over domain
`SchurVIO-CP2-timing-common-v1\0`, timing-pair index u64, common count u64, then
the increasing common camera-header timestamps as u64. The canonical payload
is retained. Every included timestamp joins exactly one serial-pair record in
each run, both joined record times satisfy warm-up, and no unilateral sample is
permitted.

## Required negative self-tests

All self-tests use synthetic files under `/tmp`, invoke no build, ROS master,
dataset registry or bag provider, and assert zero bag-provider calls.

The final stdout line of every entry point is one compact JSON object with
exact keys `schema_version`, `record_type`=`self_test_result`, `entrypoint`,
`temporary_root`, `bag_provider_calls`, `cases`, `case_count`, and `passed`.
Each case object has exact keys `index`, `name`, `expected_rejection`,
`observed_rejection`, and `passed`; indices are contiguous, names are unique,
and the one `valid_minimal_fixture` has
`expected_rejection=false,observed_rejection=false,passed=true`. Every negative
case has `expected_rejection=true,observed_rejection=true,passed=true`.
`temporary_root` is an absolute child of `/tmp` and does not exist after
return. The barrier freezes the expected ordered case name list from the
committed entry point and independently requires the mandatory names below as
a subset; a zero-case/no-op self-test cannot pass.

Every runner includes `valid_minimal_fixture`, `cli_exclusivity`,
`forbidden_bag_provider`, `non_tmp_write`, `schema_extra_key`,
`schema_missing_key`, `duplicate_json_key`, `unsafe_path`, `symlink`,
`hardlink`, `manifest_missing_entry`, `manifest_extra_entry`,
`manifest_digest_mismatch`, `readiness_order`, `readiness_timeout`,
`readiness_process_group`, `readiness_lock_identity`,
`readiness_snapshot_mutation`, `ignored_source_path`,
`snapshotted_root_symlink`, `launch_output_combination`, and
`unit_anchor_commit_mismatch`.

The recorded runner/verifier must reject at least: duplicate/noncontiguous IDs,
wrong terminal reconciliation, wrong counted category, denominator or raw-row
weight mismatch, a missing/unclassified disagreement, bad statistics
tolerance edge, missing state block, missing ordered covariance pair,
candidate-missing row omission, nonzero shadow write, baseline commit count not
one, live-preview mismatch, nonzero repair/fallback, prior/raw/config hash drift,
raw/prior layout disconnect, flipped gate decision, permuted accepted sequence,
candidate proposal copied from the baseline, proposal disconnected from its
raw system or phase-0 prior, replay no-op or skipped invocation,
phase-2/phase-3 snapshots jointly disconnected from the replayed proposal,
replay-report mismatch, nonzero internal-failure terminal, and manifest
corruption.

The sequence runner/verifier must reject at least: the exact 20 ms boundary,
nearest rather than first-forward pairing, reused image messages,
missing/duplicate pair index, callback source mismatch, identity/hash drift,
wrong mode order, coverage below 0.995, unequal shared populations,
independent alignment, invalid/non-orthogonal transform, and each metric limit.

The timing runner/verifier must reject at least: wrong pair order/index,
runtime/config/profile drift, changed clock snapshot, affinity mismatch,
warm-up boundary error, unilateral/noncommon samples, duplicate timestamp,
negative or noninteger duration, nonprimary inclusion, incorrect linear
quantiles, and each ratio limit.

No actual C/D/E artifact is eligible unless its runner self-test, the extended
independent verifier self-test, and every corruption case pass before the
artifact's first data-access command.
