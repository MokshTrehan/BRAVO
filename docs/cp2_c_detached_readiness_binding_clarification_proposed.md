# Proposed CP2-C detached-readiness and evidence-binding clarification

Status: **pending explicit approval by Moksh Trehan; non-authorizing**.

This document has not been approved and is not an operative CP2 contract. Its
creation authorizes no readiness campaign and no semantic registry, bag,
ground-truth, trajectory, timing, or other recorded-input access. No such
access is performed by this proposal. CP2-C/D/E remain fail-closed until the
approval and source binding in Section 12 are complete.

If approved, this document replaces only the contradictory readiness-schema
language identified below. It does not change estimator mathematics,
floating-point operation order, thresholds, counted populations, sequence
scope, one-pass rules, baseline authority, or CP2-C/D/E pass criteria. The
opaque-source protections of Section 11 of
`docs/cp2_c_composite_and_readiness_clarification.md` remain mandatory.

## 1. Contradiction requiring clarification

Moksh Trehan approved the Section 11 opaque source-provenance contract at
commit `b37eff6e5baa035175e1dde3cae52ee496ca9e2d`. That contract permits the
preauthorization bootstrap to handle every tracked file, including
`project/datasets.yaml`, only as opaque path, mode, size, Git-blob, and digest
provenance. Before readiness step 8 passes, tracked content may not be decoded,
searched, logged, persisted, archived, returned to another subsystem, or used
to derive a recorded-input path. In particular, an archive containing the
registry may be produced only after step 8.

The older exact schema in `docs/cp2_artifact_schema.md` cannot represent all
evidence required by that approved contract:

- its exact readiness-barrier key set omits the post-unit source recheck, held
  data-lock identity, sanitized Git environment, the exact Git-child
  population, and the nested unit-verifier execution;
- its exact self-test key set omits the working directory, process-group
  completion proof, and temporary-root removal proof;
- its source-snapshot language relies on ignore-aware Git enumeration and can
  be read to require preauthorization source-content persistence or archive
  reconstruction, while approved Section 11 requires raw untracked-name
  rejection before leaf-content access and forbids persisting registry bytes;
  and
- it does not bind the nested readiness verification to command zero of the
  later artifact.

Because schema-version-1 objects reject both missing and additional keys,
silently adding the required evidence violates the frozen schema, while
omitting it violates approved Section 11. No recorded artifact can honestly
satisfy both readings. Until this replacement is explicitly approved and
bound, the conflict is a pre-data blocker.

## 2. Replacement scope and precedence

Upon approval, Sections 3 through 11 below replace the mandatory pre-bag
readiness-barrier object, readiness self-test record, source-context/archive
binding, and readiness-command binding paragraphs of
`docs/cp2_artifact_schema.md`. All nonconflicting snapshot encodings,
provenance rules, command schemas, artifact schemas, and approved Section 11
hardening remain unchanged. If an older readiness clause permits a weaker or
different interpretation, this document's exact, fail-closed interpretation
controls.

The bootstrap remains standard-library-only until prebag authorization. A
Python readiness entry point is always executed as
`/usr/bin/python3 -I -B`; a shebang, `python3` found through `PATH`, workspace
import, user site, `PYTHONPATH`, or bytecode write is not an equivalent
execution.

## 3. Canonical source context and two archive phases

The readiness bootstrap constructs one canonical UTF-8 JSON byte string with
no trailing newline. JSON object keys are sorted, separators are exactly `,`
and `:`, non-ASCII text is not escaped merely for being non-ASCII, duplicate
keys and nonfinite numbers are impossible, and the top-level object has
exactly:

```text
branch
commit
entries
entrypoints
index_tree
record_type
schema_version
status_porcelain_v1_hex
tree
```

`schema_version` is `1`, `record_type` is
`cp2_prevalidated_unit_source_v1`, and `branch` is
`schurvio-lite/cp2-one-pass`. `commit`, `tree`, and `index_tree` are full
lowercase 40-hex identities; `tree == index_tree`; and the clean
`status_porcelain_v1_hex` is the empty string.

`entries` contains every stage-0 tracked file, including
`project/datasets.yaml`, in strict UTF-8 path-byte order. Each entry has the
exact keys `git_blob`, `mode`, `path`, `sha256`, and `size`. `git_blob` is the
stage-0 40-hex blob ID, `mode` is exactly octal `0100644` or `0100755`,
`sha256` is lowercase 64-hex, and `size` is u64. No content byte occurs in the
context. `entrypoints` contains the five frozen entry points in frozen order;
each exact record has `git_blob`, `mode`, `path`, `regular_nonsymlink`, and
`sha256`, and must equal its `entries` record with
`regular_nonsymlink=true`.

The exact canonical bytes are supplied to the nested unit verifier through
stdin and are later retained only as `readiness/source_context.json`. Their
SHA-256 is both `source_context_sha256` and the nested verifier's
`source_context_sha256`.

There are two deliberately different archive checks:

1. Before step 8 passes, the supplied unit artifact's `source_snapshot.tar`
   must be complete over every source-context entry except the exact registry
   path. The archive must contain no registry member, and the verifier must
   reject it before `extractfile` or any equivalent content-open if that
   member is present.
2. Only after step 8, the post-unit source snapshot, and the held-lock checks
   pass may the recorded runner create its full source archive. That archive
   contains every source-context entry, including the registry, but registry
   bytes remain opaque provenance at archive construction and verification.

For either permitted archive population, member paths and directory members
are exact, duplicate-free, normalized relative POSIX paths; links and special
members are forbidden; uid and gid are zero; file and directory modes are
canonical; and every global and member PAX comment equals the held commit. For
each regular member, the verifier streams and checks size, SHA-256, and the Git
blob digest over the exact bytes `blob `, decimal size, one NUL, and content.
The member and directory populations must be exact, with no missing or extra
entry.

The nested unit verifier binds every nonregistry archived source input and all
governing approval/contract files to the source context. The detached actual
verifier repeats that proof over the full postauthorization archive, including
the registry's opaque SHA-256 and Git blob. The existing frozen `SOURCE_INPUTS`
and `CONTRACT_INPUTS` populations are extended by this document and its exact
approval record. The previously approved CP2-C clarification and approval
pair, this clarification and its approval pair, every governing contract, all
five entry points, the unit report, tested commit/tree, external manifest
digest, source context, and full archive must agree. A hash, Git-blob,
population, approval, or exception mismatch fails provenance.

## 4. Exact readiness-barrier object

The replacement `readiness_barrier` object has exactly these keys and no
others:

```text
schema_version
record_type
entrypoints
source_before_sha256
build_before_sha256
source_before_payload
build_before_payload
results_before_payload
results_before_sha256
testing_before_payload
testing_before_sha256
self_tests
source_after_payload
source_after_sha256
build_after_payload
build_after_sha256
results_after_payload
results_after_sha256
testing_after_payload
testing_after_sha256
post_lock_payload
post_lock_recheck_sha256
post_unit_payload
post_unit_recheck_sha256
source_context_payload
source_context_sha256
data_lock
readiness_git_environment
readiness_git_commands
unit_verification
unit_artifact
unit_manifest_sha256
unit_tested_commit
unit_tested_tree
bag_provider_calls
passed
```

`schema_version` is `1`, `record_type` is `readiness_barrier`,
`bag_provider_calls` is integer zero, and `passed` is true. Every `*_payload`
is the unique normalized relpath specified here, and each corresponding digest
is the SHA-256 of those exact retained bytes. The existing binary snapshot
format remains unchanged. Source snapshots contain exactly the held stage-0
tracked-file records; untracked content is not a source-snapshot member.
Untracked names under `build`, `results`, and `Testing` are covered only by
their separately retained root snapshots, while an untracked name outside
those roots is fatal before its content is opened.

## 5. `readiness_git_v1` and the exact 20-command proof

`readiness_git_environment` has exactly `environment_id`, `variables`, and
`canonical_sha256`. Its ID is `readiness_git_v1`; variables are sorted by
UTF-8 name bytes and contain exactly:

```text
GIT_ALLOW_PROTOCOL=none
GIT_ATTR_NOSYSTEM=1
GIT_CONFIG_NOSYSTEM=1
GIT_LITERAL_PATHSPECS=1
GIT_NO_LAZY_FETCH=1
GIT_NO_REPLACE_OBJECTS=1
GIT_OPTIONAL_LOCKS=0
GIT_PAGER=
GIT_PROTOCOL_FROM_USER=0
GIT_TERMINAL_PROMPT=0
HOME=/tmp/schurvio-cp2-readiness-*/git-home
LANG=C.UTF-8
LC_ALL=C.UTF-8
PATH=/usr/bin:/bin
TMPDIR=/tmp/schurvio-cp2-readiness-*/git-tmp
```

The `*` is one nonempty `mkdtemp` suffix, identical in both values. During the
run, `HOME` and `TMPDIR` are distinct real mode-`0700` directories immediately
below that one real mode-`0700` private readiness root. Historical detached
verification checks this exact lexical relationship and retained digest; it
does not require the already removed directories to exist. `canonical_sha256`
hashes the frozen command-environment encoding. No inherited variable or other
`GIT_*` name is present.

Every Git child bypasses a shell, uses approved Section 11's exact argv prefix,
executes with cwd `/proc/self/fd/3`, has held root/Git descriptors 3 and 4,
starts a new process group, and is surrounded by the complete descriptor and
namespace revalidation. The command suffixes and indices are exactly:

```text
 0  ls-files --stage -z
 1  ls-files --others -z
 2  cat-file --batch
 3  status --porcelain=v1 -z --untracked-files=all
 4  ls-files --others --ignored --exclude-standard -z
 5  rev-parse --verify HEAD
 6  rev-parse --verify HEAD^{tree}
 7  merge-base --is-ancestor 8d80f483752411d34a3bc4c1ff6330b3a5c0fef3 HEAD
 8  status --porcelain=v1 -z --untracked-files=all
 9  rev-parse --verify HEAD
10  rev-parse --verify HEAD^{tree}
11  status --porcelain=v1 -z --untracked-files=all
12  rev-parse --verify HEAD
13  rev-parse --verify HEAD^{tree}
14  status --porcelain=v1 -z --untracked-files=all
15  rev-parse --verify HEAD
16  rev-parse --verify HEAD^{tree}
17  status --porcelain=v1 -z --untracked-files=all
18  rev-parse --verify HEAD
19  rev-parse --verify HEAD^{tree}
```

Indices 0 through 7 establish source identity and CP1 ancestry. Indices 8
through 10 form `source_before`; the five self-tests occur strictly between
indices 10 and 11; indices 11 through 13 form `source_after`; the data lock is
acquired before indices 14 through 16 form `post_lock`; nested unit
verification occurs strictly between indices 16 and 17; and indices 17
through 19 form `post_unit`.

Each of the 20 summaries has exactly `command_index`, `argv`, `cwd`,
`environment_sha256`, `stdin_sha256`, `stdout_sha256`, `stdout_payload`,
`stderr_sha256`, `started_utc`, `finished_utc`, `exit_code`, `timed_out`, and
`process_group_complete`. Indices are contiguous; intervals are nonnegative
and serialized; the environment digest is `readiness_git_v1`; exit is zero;
`timed_out=false`; `process_group_complete=true`; and stderr is empty. Stdin is
empty except for index 2, whose digest covers the ordered `<blob-id>\n`
request.

For every index except 2, `stdout_payload` is exactly
`readiness/git/NN.stdout`; the summary contains only that relpath and its
digest, never inline output. Index 2 has `stdout_payload=null`. Its complete
`cat-file --batch` content flows directly through a bounded parser and digest
accumulator: it is never retained in a temporary file, returned as a byte
buffer, logged, attached, archived, or otherwise persisted. Only each blob's
size/SHA-256 identity and the whole stdout digest survive. The detached
verifier independently reconstructs that digest from the full authorized
source archive. Thus the retained Git-output population is exactly 19 path
attachments, not 20.

## 6. Exact readiness self-tests

The five self-tests run sequentially in frozen entry-point order under the
exact environment:

```text
PATH=/usr/bin:/bin
LANG=C.UTF-8
LC_ALL=C.UTF-8
CP2_SELF_TEST=1
CP2_FORBID_BAG_ACCESS=1
```

The canonical encoding of that complete five-variable map is retained as the
single provenance environment class `readiness_self_test_v1`; every self-test
record has its digest. No `commands.jsonl` row or non-self-test process may
reference that class or contain either `CP2_*` variable.

Each committed entry point is executed through its already held descriptor.
A Python argv is exactly
`["/usr/bin/python3","-I","-B","/proc/self/fd/N","--self-test"]`; the
shell entry point argv is exactly
`["/proc/self/fd/N","--self-test"]`, with that descriptor also used as the
executable. `N` is a valid inherited descriptor of at least 3. Cwd is exactly
`/tmp`, stdin is `/dev/null`, no shell is involved, and the timeout is 300
seconds.

Each child starts a new session/process group. Timeout or any surviving group
causes termination, kill if necessary, complete reap, and barrier failure.
The final record requires `timed_out=false` and
`process_group_complete=true`.

Each self-test record has exactly:

```text
index
path
argv
cwd
entrypoint_sha256
started_utc
finished_utc
environment_sha256
exit_code
timed_out
process_group_complete
stdout
stdout_sha256
stderr
stderr_sha256
expected_case_names
result
bag_provider_calls
temporary_root_removed
```

`cwd` is `/tmp`, exit is zero, bag-provider calls are integer zero, and
`temporary_root_removed=true`. Streams are the distinct exact paths
`readiness/self_tests/NN.stdout` and `.stderr`. The final LF-terminated stdout
line is parsed as the existing exact `self_test_result`; its `temporary_root`
must be an absolute direct child of `/tmp` and must no longer exist. Case
inventory, order, index, expected-rejection polarity, observed outcome, and
aggregate pass remain exact.

## 7. Data lock and post-unit source identity

After byte-identical before/after source, build, results, and Testing snapshots
and after the self-tests, the runner opens exactly
`/tmp/schurvio-lite-cp2-data.lock` with
`O_CREAT|O_RDWR|O_CLOEXEC`, requires a regular nonsymlink, single-link,
effective-user-owned mode-`0600` file, and acquires `flock(LOCK_EX|LOCK_NB)`.
The `data_lock` record has exactly `path`, `device`, `inode`, `mode`,
`owner_uid`, `link_count`, and `acquired_exclusive`; the last value is true.
The live descriptor and path binding must equal this record and remain held
through hidden-artifact validation and final no-overwrite publication.

These are producer lifetime requirements. A detached verifier validates the
retained exact record, its chronology, and its joins; it must not reopen,
`lstat`, lock, or require the global lock path/inode to persist after the
producer exits. A later unrelated lock file cannot strengthen or invalidate
historical evidence.

`post_lock_payload` is exactly `readiness/post_lock.bin`. After nested unit
verification completes, the runner performs the final Git triplet and writes
`post_unit_payload` exactly as `readiness/post_unit.bin`. Both payloads use the
existing source-snapshot encoding and root tag `post_lock`; they are
byte-identical. Their commit, tree, index tree, empty status, tracked
population, and five entry-point identities equal `source_before`,
`source_after`, the canonical source context, and the unit anchor. Only then is
`prebag_authorized=true`.

The registry may then be opened exactly once by the approved Section 11
descriptor-relative procedure, and only after its size/SHA-256 equals the
opaque source-context record. No registry-derived path is resolved, statted,
hashed, or opened before that equality succeeds.

## 8. Nested unit verification and command-zero binding

Before execution, the external unit artifact is copied without links or
special files into the readiness private root as
`unit-artifact-frozen`; every source binding is checked during the copy, total
and per-file sizes are bounded, files are finalized read-only, and the copied
`SHA256SUMS` bytes equal the externally supplied manifest digest.

The held committed verifier is invoked without a shell as exactly:

```text
/usr/bin/python3 -I -B /proc/self/fd/N
--verify-unit-anchor-prevalidated
/tmp/schurvio-cp2-readiness-*/unit-artifact-frozen
--expected-manifest-sha256 <unit_manifest_sha256>
```

Its cwd is `/tmp`; its environment is exactly `PATH=/usr/bin:/bin`, `LANG=C`,
and `LC_ALL=C`; canonical source-context bytes are stdin; it starts a new
process group and has a 1,800-second timeout. Its retained exact record has
`argv`, `cwd`, `environment`, `started_utc`, `finished_utc`, `exit_code`,
`timed_out`, `process_group_complete`, `stdout`, `stdout_sha256`, `stderr`,
`stderr_sha256`, and `source_context_sha256`. Stream paths are exactly
`readiness/unit_verifier.stdout` and `.stderr`. Exit is zero, timeout is false,
the group is complete, and the final stdout result has exactly `commit`,
`passed`, `record_type`, `schema_version`, and `tree`, with record type
`cp2_prevalidated_unit_verification_result`, schema 1, pass true, and the held
commit/tree.

The nested verifier independently checks the external manifest, unit report,
complete 203-test CP1/CP2 inventory, strict-FP build and linkage evidence,
corruption self-test, nonregistry source archive, source Git blobs, approvals,
contracts, and exact tested commit/tree. A successful child report is not
trusted without those checks.

The detached actual verifier's only workspace-local executable Python helper
modules are `scripts/cp2/cp2_schema.py` and
`scripts/cp2/cp2_sequence_math.py`. Before either can load in production
verification, its exact path, Git mode/executable bit, size, and SHA-256 are
installed from the already validated canonical source context. The loader
allowlists only those two names, opens the real `scripts/cp2` directory and
leaf with descriptor-relative `O_NOFOLLOW`, requires a single-link regular
file, reads and re-fstats the same descriptor, checks the context size/hash and
mode, compiles the held bytes directly, and never calls a path-reopening loader.
A cache hit must retain the same source binding. The artifact-free verifier
self-test uses a separately marked live-fixture binding; unbound production
loading fails.

In every later actual artifact, `commands.jsonl` contains exactly one
`phase=readiness` row, it is command ID zero, it is the first row, and its
`sequence_index`, `pair_index`, and `run_index` are null. Its argv, cwd,
interval, exit, timeout, stdout/stderr paths and hashes are byte-for-byte equal
to `unit_verification`; its environment digest names the exact
`unit_verifier_v1` class formed from the three variables above. No second
readiness row or alternate copy is permitted.

## 9. Closed readiness namespace

The finalized artifact's `readiness/` regular-file namespace is exactly:

```text
readiness/barrier.json
readiness/source_context.json
readiness/source_before.bin
readiness/build_before.bin
readiness/results_before.bin
readiness/testing_before.bin
readiness/self_tests/00.stdout
readiness/self_tests/01.stdout
readiness/self_tests/02.stdout
readiness/self_tests/03.stdout
readiness/self_tests/04.stdout
readiness/self_tests/00.stderr
readiness/self_tests/01.stderr
readiness/self_tests/02.stderr
readiness/self_tests/03.stderr
readiness/self_tests/04.stderr
readiness/source_after.bin
readiness/build_after.bin
readiness/results_after.bin
readiness/testing_after.bin
readiness/post_lock.bin
readiness/post_unit.bin
readiness/git/00.stdout
readiness/git/01.stdout
readiness/git/03.stdout
readiness/git/04.stdout
readiness/git/05.stdout
readiness/git/06.stdout
readiness/git/07.stdout
readiness/git/08.stdout
readiness/git/09.stdout
readiness/git/10.stdout
readiness/git/11.stdout
readiness/git/12.stdout
readiness/git/13.stdout
readiness/git/14.stdout
readiness/git/15.stdout
readiness/git/16.stdout
readiness/git/17.stdout
readiness/git/18.stdout
readiness/git/19.stdout
readiness/unit_verifier.stdout
readiness/unit_verifier.stderr
```

There is deliberately no `readiness/git/02.stdout`.

Every path is normalized, unique, present in `SHA256SUMS`, and present in the
provenance file inventory with role `readiness` and mode `0444`. Every path is
claimed by its exact barrier field. The unit-verifier streams are additionally
named by the deliberately identical command-zero row; this is one binding to
the same two files, not authorization for aliases or copies. The set of
manifest paths beginning `readiness/` equals the set above exactly. Missing,
orphan, additional, relocated, cross-aliased, symlinked, hardlinked, or
nonregular entries fail detached verification.

## 10. Failure cleanup and lifetime

On any failure before prebag authorization, the runner terminates and reaps
every owned process group, closes all inherited and held source/Git/artifact
descriptors, unlocks and closes the data lock if acquired, removes the entire
private readiness root and all partial attachments, returns no authorization,
leaves no result directory, and retains zero bag-provider calls. Cleanup
failure is itself reported and cannot convert the original failure into an
authorization.

The same BaseException-wide lifecycle applies to recorded commands and the
detached offline-replay child. Timeout, wait failure, later stream/path/hash
validation failure, `KeyboardInterrupt`, and `SystemExit` all trigger
session-wide SIGKILL when necessary, leader reaping, and a bounded proof that
the process group is absent before the temporary root is removed. A normally
exited leader that left a descendant is a verification failure.

On success, the authorization object continues to own the repository
bindings, lock descriptor, frozen unit copy, and private readiness root. It
revalidates them before recorded access and keeps them alive through atomic
artifact finalization. Closing or losing any binding revokes authorization;
it is not recoverable by reopening a path.

After finalization or any exit, the producer closes those bindings and removes
the frozen copy and private readiness root. Detached verification of the
finalized or historical artifact validates only retained bytes, digests,
metadata, chronology, and lexical/path joins. It must not require an ephemeral
readiness directory, frozen-copy path, inherited descriptor, process group, or
global lock inode to remain live.

## 11. Mandatory protecting and corruption tests

Before any CP2-C readiness attempt, synthetic `/tmp` tests with zero recorded
access must prove rejection of at least:

- missing, extra, duplicate, reordered, noncanonical, nonfinite, or
  hash-mismatched source-context fields and entries;
- a preauthorization unit archive containing the registry; a missing, extra,
  linked, special, unsafe, duplicate, wrong-mode, wrong-owner, wrong-PAX,
  wrong-size, wrong-SHA, or wrong-Git-blob archive member; and any full-archive,
  approval-pair, exception-list, or contract mismatch;
- a missing/extra/reordered Git command, altered prefix or suffix, inherited or
  altered environment, changed cwd, wrong stdin/stdout/stderr digest, nonzero
  exit, timeout, overlapping chronology, surviving process group, retained
  `cat-file` content, a nonnull index-2 stdout path, or an absent/orphan one of
  the 19 permitted Git stdout attachments;
- a self-test without `/usr/bin/python3 -I -B`, a descriptor/path identity
  mismatch, non-`/tmp` cwd, environment drift, overlapping order, timeout,
  surviving child/grandchild, non-direct or surviving temporary root, stream
  mismatch, false/skipped/reordered case, or nonzero provider call;
- a data-lock symlink, hardlink, wrong owner/mode/type, contention, path
  replacement, descriptor mismatch, premature release, or post-lock mutation;
- a missing, changed, wrongly tagged, or nonidentical post-unit snapshot;
- a mutable or malformed frozen unit copy, external-manifest mismatch, altered
  nested argv/environment/stdin/result/commit/tree, timeout or surviving group,
  nested verifier no-op, or failed 203-test/corruption/provenance proof;
- a missing, duplicate, nonzero, nonfirst, or nonidentical command-zero binding;
  an unreferenced readiness file; a missing manifest/inventory reference; or a
  namespace alias, relocation, extra file, link, or writable final mode; and
- injected failure at every acquisition/publication boundary, proving complete
  process, descriptor, lock, private-root, partial-artifact, and provider-call
  cleanup, with spies proving no registry parser, provider, bag, ground-truth,
  trajectory, or payload access before authorization.

These tests are unit/readiness protection only. They create no CP2-C evidence
and cannot authorize recorded access.

## 12. Approval and source binding

This proposal must first be committed while retaining its pending,
non-authorizing status. Any approval must explicitly name that exact reviewed
commit, identify this document, state that Moksh Trehan approves the complete
replacement, and state **no exceptions**. A statement that omits the exact
commit or admits an exception is not approval.

The pending review commit must keep both CP2-C actual entry paths hard blocked:
the recorded runner returns before loading the readiness module or obtaining a
registry capability, and the detached recorded-artifact verifier returns before
scanning the artifact or following any retained input path. The protecting test
must inject failing loader/scanner sentinels and prove neither is reached. Only
the later approval-binding commit described below may deliberately remove those
blocks, and only after it has added every approval/source identity required by
this section.

After such approval, a separate commit records it in
`project/cp2_c_detached_readiness_binding_approval.json` with the same exact
approval-record discipline already frozen for CP2-C. Its exact keys are
`schema_version`, `record_type`, `reviewer`, `approved_utc`,
`reviewed_commit`, `document_path`, `document_git_blob`, `document_sha256`,
`approval_statement`, and `exceptions`; its record type is
`cp2_c_detached_readiness_binding_approval`, reviewer is `Moksh Trehan`, the
document path is this file, and `exceptions` is an empty array. That separate
commit may update the gate pointer but must not edit the reviewed document.

Every later unit, readiness, recorded, and detached-verification path must add
this document and that approval record to the governing contract/source
inventories and freeze both SHA-256 and Git-blob identities. Missing bytes,
wrong identity, a nonempty exception list, an unbound implementation commit,
or omission from the source context, permitted archives, provenance,
manifest, or detached verifier fails before recorded-data access.
