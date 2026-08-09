# Proposed CP2-C composite, oracle, and readiness clarification

Status: **pending explicit approval by Moksh Trehan**.

This document is non-operative until that approval is recorded against an
exact commit. Until then, CP2-C composite-state implementation, recorded-data
readiness, registry interpretation, and all bag, ground-truth, and payload
access remain fail-closed.

This status line is intentionally immutable. After approval, the separate
approval record and `project/cp2_gate.yaml` carry the operative status; this
reviewed document is not edited merely to change `pending` to `approved`.

Upon approval, this document clarifies the frozen CP2 contracts. It fixes
units, identities, validation rules, phase failure handling, and the one
source-provenance exception needed by the readiness barrier. It does not alter
the estimator equations, numerical operation order, thresholds, baseline
authority, accepted canonical field inventory, or CP2-C/D/E pass criteria.
Where an older clause admits more than one reading, the stricter reading here
is normative.

## 1. Phase ownership, capture, and failure atomicity

Take exactly one tentative phase-0 capture at the existing immutable-prior
boundary after cleaning and triangulation and before the first raw Jacobian
assembly, reduction, gate, or candidate operation. When traversal first
determines that a shared raw system can be assembled, completely encode and
validate the tentative capture before assembling or counting that system. On
success, promote the already validated bytes/token to phase 0 and then
assemble the first raw system. On failure, no raw system is assembled or
counted, no phase is emitted, and the out-of-band run-fatal trace latch stops
the runner and discards the hidden partial campaign; the frozen schema has no
honest feature-row encoding for a raw system whose reduction and gate were
never reached. If traversal produces zero raw systems, discard the tentative
capture without phase validation or emission. Invalid tentative data is never
supplied to raw assembly, reducer, gate, preview, or detached-oracle
arithmetic.
Every baseline/candidate gate and preview prior is projected from the promoted
composite; no path performs a second live-state read to populate its prior.

The exact committing order is:

1. complete raw traversal, both path lifecycles, and every available proposal;
2. construct, validate, and encode phase 2 from phase 0 and the accepted live
   baseline preview into an unpublished owning buffer;
3. capture phase 1 with the same adapter and verify phase-0/phase-1 canonical
   values and the in-memory pointer graph; and
4. with no intervening live-state read, evidence arithmetic, logging, callback,
   or estimator operation, enter the sole baseline commit.

For a noncommitting terminal with `raw_system_count>0`, capture phase 1
and validate it, sample the steady-clock endpoint as the last timed operation,
publish through the authoritative sink, and return; do not emit phase 2. A
zero-raw-system terminal emits no phase and follows endpoint, sink, return. If
phase 2 was tentatively built but the commit is suppressed, discard its
unpublished buffer because the frozen schema forbids phase 2 on a noncommitting
record.

After `StateHelper::EKFUpdate` returns, the mandatory
`std::chrono::steady_clock` committing endpoint remains the first operation.
The allocation-free phase-3 fill is the next operation and occurs before any
other estimator computation or mutation. Phase-3 capture, encoding, and sink
work is therefore excluded from `duration_ns`. All captures execute on the
same serialized state processing path under the same state-ownership
discipline.

Phase 0 exists only after its complete encoding and validation succeeds under
the promotion rule above. A tentative capture that cannot be encoded or is
invalid takes that rule's run-fatal, no-artifact path; it is never represented
as an ordinary failed update. After a valid phase 0, a complete phase 1 is
compared bitwise, structurally, and by pointer graph before any independent
phase-1 validity classification. Any difference is
`internal_failure/snapshot_mismatch`, including a finite phase-0 value changed
to a nonfinite phase-1 value. If phase 1 is identical to valid phase 0, it is
valid by construction. A complete but invalid phase 2 maps to
`internal_failure/trace_invariant_failure`, suppresses the commit, and is not
emitted.

Every ordinary precommit failure suppresses `StateHelper::EKFUpdate`, has zero
baseline transaction-level mean/covariance commits, and has zero candidate
writes. A tentative-promotion failure performs no live State
mean/covariance/calibration/cache/map write after the tentative capture. An
invalid phase-2 path performs none after phase 0. A `snapshot_mismatch`, by
contrast, is proof that an unauthorized precommit live mutation or replacement
may have occurred; it fails `math_passed` without falsely asserting zero such
writes. None of these rules claims to undo baseline feature
cleaning/triangulation/lifecycle work completed before the tentative capture.

If any required phase or update trace cannot be encoded completely, it cannot
be represented by the exact schema. Before or after commit, such a failure sets
the out-of-band run-fatal trace latch, stops the runner, discards the hidden
partial campaign, and finalizes no schema-version-1 artifact.

All exact storage required for phase-3 capture and its value-owning handoff is
allocated and validated before the commit. A narrow read-only State friend
adapter and const/noexcept, allocation-free `CamBase` cache accessors are
explicitly permitted and required solely to expose the already frozen fields
directly into pre-sized storage. The existing allocating `CamBase::get_value()`
is not used by phase 3, and phase-0 cache bytes are never substituted for a
live phase-3 read. These accessors do not mutate state, add a canonical field,
or alter estimator arithmetic.

The postcommit capture/fill/handoff boundary is allocation-free and
nonthrowing: no exception escapes it, and a caught failure sets the fatal
latch. The trusted recorded sink returns explicit status and cannot merely log
and suppress publication failure. The generic diagnostic observer may retain
its existing exception-containment behavior, but it is not the authoritative
recorded sink. A complete phase-3 payload that is
structurally valid but differs from phase 2 preserves the live
`committed_counted` taxonomy, fails the commit oracle, increments the aggregate
committed-update mismatch count, and sets `math_passed=false`; the baseline is
never rolled back or relabeled.

If phase-3 capture, encoding, or required sink publication cannot produce the
complete schema-defined record after `EKFUpdate` returned, the baseline remains
committed and the general incomplete-trace rule above applies. An incomplete
postcommit record is not represented as ordinary failed evidence and is never
credited as CP2-C.

## 2. In-memory pointer-graph proof

At phase 0, capture and retain an owning, in-memory-only pointer-graph token.
It covers, in declared order:

- `State::_variables` and the unique live IMU;
- every clone map key/value and each clone quaternion/position subvariable;
- every active SLAM map key/value;
- the IMU pose, quaternion, position, velocity, gyro-bias, and accel-bias
  subvariables; and
- every fixed IMU-calibration, time-offset, per-camera calibration,
  per-camera-intrinsics, and camera-model-cache pointer used by the composite.

All required pointers are nonnull. Semantically distinct active and fixed
roles have distinct object addresses; only the declared IMU/Pose parent-to-
subvariable ownership relationships may share one ownership graph. Membership
is bijective: `_variables` contains exactly one IMU plus every clone-map value
and every active SLAM-map value, each once, with no additional active object.
Every inactive calibration `Type` has local ID `-1` and is absent from
`_variables`. The owning token prevents address reuse from concealing
replacement.

Phase 1 requires identical vector order, map key sets, key-to-object
associations, subvariable associations, and fixed-calibration/cache
associations. Phase 3 applies the same identity check. Raw pointer values and
the token are never serialized, logged, or hashed; they are a same-process
zero-write proof only. A precommit mismatch is `snapshot_mismatch`. A
postcommit pointer-graph mismatch has no exact per-update schema field, so it
sets the out-of-band run-fatal latch and produces no artifact under Section 1;
it is not folded into `baseline_commit_mismatches`. A complete phase-3 value
or bit mismatch remains representable failed evidence as defined there.

## 3. Active-state partition and internal consistency

Let `n` be the phase-0 covariance dimension. The covariance is square. The
live `_variables` sequence contains nonnull, positive-size top-level types in
strictly increasing nonnegative covariance-ID order. Their half-open intervals
`[id,id+size)` partition `[0,n)` exactly. Canonical capture uses this live
order; malformed live metadata is rejected rather than repaired by sorting.

The semantic-block list is built from actual production subvariable IDs and
sizes and also partitions `[0,n)` exactly:

- IMU `theta`, position, velocity, gyro bias, and accelerometer bias at the
  actual `q`, `p`, `v`, `bg`, and `ba` IDs, each of size three;
- clone `theta` and position at the actual quaternion and position IDs, each
  of size three; and
- one full block for each active SLAM landmark at its top-level ID and error
  size.

The list is strictly covariance-ID ordered. Each IMU or clone top-level
nominal/FEJ matrix must be bitwise identical to the exact concatenation of its
production subvariable nominal/FEJ matrices. Parent/subvariable layout or byte
disagreement is a trace invariant failure. Camera and IMU calibration types
remain inactive under the frozen profile, and no unknown active type is
permitted.

## 4. Clone identity

Strict clone timestamp increase is evaluated once per unique top-level clone
entity in covariance-ID order, not once per semantic block. Each clone emits
exactly two adjacent blocks, `clone.theta` followed by `clone.position`; both
repeat the identical timestamp bits.

Clone identity is the `_clones_IMU` map key. Every clone timestamp is finite.
Consecutive clone entities satisfy strict numeric `<`. Numerically equal
timestamps, including `-0.0` and `+0.0`, are duplicate identities and invalidate
capture even though their bit patterns differ. Valid timestamp bits are
retained unchanged.

## 5. Landmark identity and anchor metadata

Landmark identity uses the `_features_SLAM` map key, which equals the stored
`Landmark::_featid`. Feature IDs are unique. The canonical signed camera field
is `_anchor_cam_id`, never `_unique_camera_id`. `UNKNOWN` representation is
invalid. `ANCHORED_INVERSE_DEPTH_SINGLE` has error size one; every other valid
representation has error size three.

For `GLOBAL_3D` and `GLOBAL_FULL_INVERSE_DEPTH`, anchor metadata has no
geometric referent and uses exactly:

```text
anchor_camera_id = -1
anchor_timestamp_bits = bff0000000000000  # binary64(-1.0)
```

For every anchored representation, `_anchor_cam_id` is nonnegative and belongs
to the configured camera-ID set. Its finite anchor timestamp bitwise matches
exactly one active clone-map key. No numeric-near or converted timestamp match
is accepted. Representation and anchor sidecar metadata remains unchanged
through the commit oracle.

## 6. Detached commit oracle

The online phase-2 oracle starts only from phase 0 and the accepted live
baseline preview produced from that phase. The offline oracle starts from
phase 0 and the independently replayed baseline proposal. Exact canonical
proposal-payload equality binds those inputs; offline replay never supplies a
future value to the live precommit path.

Instantiate one detached clone-equivalent production object per active
top-level type in ascending covariance-ID order. IMU and clone entries use
`ov_type::IMU` and `ov_type::PoseJPL`. A SLAM landmark uses the inherited
`ov_type::Vec(error_size)` clone/update behavior; its representation, feature
ID, anchor camera, and anchor timestamp remain an immutable sidecar and are not
inferred from or erased by the detached object.

Set each detached object's phase-0 nominal and FEJ matrices and local ID. Apply
its complete `dx[id:id+size]` segment exactly once through production
`Type::update`. No subvariable-by-subvariable update, representation
conversion, alternate retraction, or second update call is permitted.

The expected phase-2 composite contains the detached updated nominal matrices,
unchanged phase-0 FEJ matrices and identities, and role-bound baseline
`P_plus`: the accepted live baseline-preview value online and the independently
replayed baseline-proposal value offline, joined by exact proposal-payload
equality. It also contains unchanged phase-0 timestamp, semantic inventory,
fixed calibrations, camera calibrations, and camera caches. Its canonical bytes
equal both the retained phase-2 payload and the complete live phase-3 payload.
Offline reconstruction follows this same factory and operation order.

## 7. Exact counter units and equality

A `binary64 coefficient` is one scalar slot of a canonical matrix, independent
of Eigen storage order. Equality means equality of all 64 IEEE-754 bits;
floating-point `==` is not used. `+0.0` and `-0.0` therefore mismatch. Equal
NaN payloads can be bitwise equal but still fail Section 8.

For a structurally valid committed update:

- `baseline_expected_type_update_calls` is the number of active top-level
  phase-0 types;
- `baseline_verified_nominal_fields` is the sum of `rows*cols` over all
  top-level nominal matrices compared between phases 2 and 3; every checked
  coefficient is counted whether it matches or not;
- `baseline_nominal_mismatches` counts unequal nominal coefficient bits;
- `baseline_covariance_mismatches` counts unequal coefficient bits over the
  complete ordered `n`-by-`n` covariance, including both triangles; and
- `baseline_fej_mismatches` counts unequal coefficient bits between every
  phase-3 active top-level FEJ matrix and its phase-0 value.

`baseline_verified_nominal_fields` is not an error-state-DOF, matrix, byte, or
semantic-block count. The FEJ population has the same shapes and coefficient
count. Timestamp, fixed calibration, camera-cache, block-inventory, and
identity equality is enforced by complete canonical-payload equality and is
not folded into those three mathematical mismatch counters.

An inventory, identity, or shape mismatch is not coerced into a coefficient
mismatch. No partial population is counted: the verified-field and three
coefficient-mismatch counters are zero, the commit-oracle structural check
fails, and `math_passed=false`. On every noncommitting terminal, expected,
verified, and mismatch counters are zero.

Campaign `baseline_commit_mismatches` counts committed updater invocations
having any structural, finiteness, complete-payload, nominal, covariance, FEJ,
or other commit-oracle failure representable by a complete phase-3 payload and
the existing update fields. It excludes pointer-graph and incomplete-trace
failures, which discard the partial campaign under Sections 1 and 2. It is an
update count, not a coefficient sum. `state_blocks_expected/seen` and
`covariance_blocks_expected/seen` count complete row presence, summed as `B`
and `B*B` over committed records, independently of numeric comparison
availability.

## 8. Finite-state requirements and failed binary evidence

Canonical snapshot binary encoding is lossless over every binary64 bit
pattern. It preserves signed zero, subnormals, infinities, and NaN payload/sign
bits without normalization. JSON never contains NaN, infinity, or a string
substitute; unavailable or nonfinite JSON diagnostics remain null under the
existing schema.

Lossless encodability is not validity. A passing CP2-C record requires every
binary64 coefficient or scalar in phases 0, 1, 2, and 3 to be finite. This
includes timestamps, covariance, active nominal/FEJ matrices, fixed
calibration matrices, camera calibration/cache matrices, clone timestamps, and
non-sentinel anchor timestamps. The global `-1.0` anchor sentinel is finite.

Every JPL quaternion-bearing field is also a valid unit quaternion. This covers
the exact four-coefficient quaternion slice of the active IMU nominal/FEJ,
every clone nominal/FEJ, fixed `_calib_imu_GYROtoIMU` and
`_calib_imu_ACCtoIMU` nominal/FEJ, and every fixed camera-extrinsic `PoseJPL`
nominal/FEJ. In the strict-FP evidence translation unit, evaluate
`s=q.squaredNorm()` and then require `s` finite and
`std::abs(s-1.0) <= 32*std::numeric_limits<double>::epsilon()`. Equality at the
bound passes; the immediately larger representable error fails. No
normalization, sign change, clamping, or repair is permitted for this check.

A nonfinite or invalid-quaternion tentative phase-0 field is retained by exact
bits only in the validator's private buffer and takes the run-fatal,
no-artifact promotion-failure path in Section 1. A phase-1 field that differs
from valid phase 0 is a `snapshot_mismatch`; exact-bit comparison detects it
without supplying it to preview or detached-update arithmetic. A nonfinite or
invalid-quaternion phase-2 field is a precommit failure. A complete, encodable
phase-3 payload containing such a value preserves
`committed_counted`, fails the commit oracle, and makes the campaign fail even
if its bits match another nonfinite value. An incomplete phase-3 capture uses
the run-fatal rule in Section 1.

## 9. Retained-gamma meaning

On every feature, state-block, and covariance-block child row,
`baseline_retained_gamma` and `candidate_retained_gamma` are referential copies
of the joined updater invocation's final ordered accepted-feature gamma totals.
They are never per-feature contributions and never running prefixes.

`baseline_retained_gamma` bitwise equals `updates.baseline_gamma` when
`baseline_gamma_status=available` and is null otherwise.
`candidate_retained_gamma` bitwise equals `updates.candidate_gamma` when the
candidate accepted-list traversal completed with a finite ordered total,
including exact positive zero for an empty accepted list, and is null exactly
when the update value is null on its nonfinite, internal, or not-reached path.
Per-feature mathematical contributions remain exclusively in
`nullspace_gamma` and `schur_gamma`; lifecycle gate acceptance plus the accepted
processing sequence determines whether each contribution belongs to the
ordered total.

## 10. Checked integer arithmetic and ratios

Every u64 count, size, product, and payload offset/length accumulation is
checked before arithmetic. Overflow is `trace_invariant_failure`; wraparound,
saturation, and clamping are forbidden. This includes `rows*cols`, `B*B`,
coefficient populations, aggregate expected/seen counts, and file framing.

Agreement gates use exact nonwrapping integer cross-products for the normative
`1000*numerator >= 999*denominator` decisions. Implementations use an exact
integer type wide enough for the validated operands; a wrapped u64 product is
never accepted. Every numerator and every nonzero denominator converted for a
stored diagnostic ratio is at most `2^53-1`. Larger values fail evidence rather
than undergo an inexact conversion. The C++ producer, offline replay, and
independent verifier require `FE_TONEAREST` immediately before each ratio
operation, not only at process startup. The ratio is one binary64 conversion
per operand followed by one binary64 division. A zero denominator retains the
schema-defined null ratio and fails any required nonempty population.

## 11. Opaque source-provenance exception

Before the first Git subprocess, the standard-library bootstrap opens the
already known repository root and then `.git` descriptor-relative with
`O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC`. `.git` must be a real directory;
a symlink, gitfile, or `commondir` is fatal. Relative to the held Git-directory
descriptor, `config` and `index` must open with
`O_RDONLY|O_NOFOLLOW|O_CLOEXEC` as regular, single-link files owned by the
Git-directory owner and not world-writable. Their descriptors remain open.
The bootstrap records and hashes their bytes and parses the held `config`
bytes with a bounded, side-effect-free Git section/key/boolean grammar that
never resolves an include, expands a path, or executes a value; malformed or
unsupported syntax fails closed. Case-folded keys reject every dangerous
configuration class named below as well as any `core.worktree`, true
`core.bare`, non-true `core.filemode`, true `core.sparseCheckout`, true
`core.sparseCheckoutCone`, true `core.ignoreCase`, true
`extensions.sparseIndex`, or true `extensions.worktreeConfig`.
`config.worktree` must be absent. Before and after every Git subprocess, the
held `config` and `index`
descriptors and their descriptor-relative path bindings must retain identical
device, inode, link count, mode, owner, size, nanosecond mtime/ctime, and
SHA-256. `commondir` and `config.worktree` must remain absent. This preflight
therefore fixes the repository and worktree identity before Git can discover
either one.

It also validates the binary index itself before status: every entry is stage
0 with a regular-file Git mode, no entry is a gitlink, and no `link`
split-index extension, skip-worktree flag, or sparse-directory entry is
present. The bootstrap records the repository-root
and Git-directory device, inode, link count, mode, owner, size, and nanosecond
mtime/ctime. Before and after every Git subprocess, `fstat` of both held
descriptors and `fstatat(...,".git",AT_SYMLINK_NOFOLLOW)` relative to the held
root must prove that every field is unchanged and `.git` is the same real
directory as the held Git descriptor. For the child, the bootstrap duplicates
the held root and Git-directory descriptors to fixed inherited descriptors 3
and 4, clears `FD_CLOEXEC` only on those child duplicates, calls `fchdir(3)`
after fork and before exec, and supplies the fixed descriptor paths in the Git
argv below. Git 2.25 may canonicalize those descriptor paths to a pathname;
the normative guarantee is that the descriptors bind the initial identity and
any later unprivileged path replacement or mutation changes a locked directory
ctime or another tuple field, makes the run fatal, and finalizes no artifact.

Before the first Git subprocess, a descriptor-relative walk of the complete
non-object Git administrative namespace (the held `.git` tree excluding only
the separately checked `objects` subtree) opens every directory with
`O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC`. Every nondirectory entry must be
a regular, single-link, non-world-writable file owned by the Git-directory
owner; symlinks and special files are fatal. Its ordered path/type/mode/owner/
device/inode/link-count/size/mtime/ctime metadata snapshot is unchanged after
every Git subprocess. `info/sparse-checkout` must be absent before and after
every subprocess.

The walk additionally opens and holds `HEAD`, the exact loose ref named by its
strictly parsed symbolic-ref bytes, and `packed-refs` when present. For this
campaign, the `HEAD` bytes are exactly
`ref: refs/heads/schurvio-lite/cp2-one-pass` followed by one LF; every ref path
component is opened descriptor-relative without following symlinks. The held
control files are regular, single-link, owned by the Git-directory owner, not
world-writable, and their full identity/time/size/SHA-256 tuples and path
bindings are unchanged before and after every Git subprocess.

During readiness steps 2, 3, 5, and 7 only, the standard-library bootstrap may
enumerate HEAD/index/status metadata and process each stage-0 tracked source
entry, including `project/datasets.yaml`, solely as opaque source provenance.
Using an exact-byte prefix trie from the already parsed index, it first performs
a standard-library, descriptor-relative namespace/type prewalk without reading
any file content or following any symlink. The bound repository-root `.git` is
the sole skipped administrative entry. Outside the exact first components
`build`, `results`, and `Testing`, any path not equal to an index leaf or a
prefix of an index leaf is fatal immediately and an untracked directory is
never entered. Under the three allowed roots the prewalk recursively enumerates
names and types without following symlinks. An untracked entry whose final
component is `.gitignore` or `.gitattributes` is fatal everywhere, including
under those roots; every non-root entry whose final component is `.git` is
fatal whether tracked or untracked.

The bootstrap holds every directory traversed by that prewalk, records its
device, inode, link count, mode, owner, size, and nanosecond mtime/ctime, and
revalidates every tuple and descriptor-relative binding before and after each
later worktree-walking Git subprocess. It then completes the tracked-path
descriptor/mode walk defined below without reading any leaf content and retains
all descriptors. Only after both walks pass does it enumerate raw other-path
names with the fixed `git ls-files --others -z` operation, without any exclude
or ignore-rule option, and revalidate all held namespace and tracked-path
tuples and bindings.
Before reading any tracked or untracked leaf content, it rejects every such raw
other path whose first path component is not exactly `build`, `results`, or
`Testing`.
It next completes the opaque tracked blob/worktree hashing defined below. Only
then does it perform the fixed sanitized status and ignored-population
operations;
those operations may inspect tracked bytes solely inside the opaque Git
boundary defined below. They never open untracked file content. Any untracked
source entry is therefore fatal before its content is opened, while the three
allowed roots remain subject to their separate frozen snapshots.

Every Git subprocess uses environment class `readiness_git_v1`. For that class
only, this clarification extends the common allowed environment-name inventory
by exactly these names and values:

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
```

No other environment class contains a `GIT_*` variable. The class additionally
contains only the already-allowed `HOME`, `LANG`, `LC_ALL`, `PATH`, and
`TMPDIR`: `HOME` and `TMPDIR` name precreated fresh private temporary
directories, `LANG=LC_ALL=C.UTF-8`, and `PATH=/usr/bin:/bin`. Its complete
actual variables and canonical digest are retained under the existing
environment schema.

Every readiness Git command references that class, bypasses a shell, and starts
with this exact argv prefix in this exact order:

```text
/usr/bin/git --no-pager --no-optional-locks
--git-dir=/proc/self/fd/4
--work-tree=/proc/self/fd/3
-c color.ui=false
-c core.attributesFile=/dev/null
-c core.commitGraph=false
-c core.excludesFile=/dev/null
-c core.fileMode=true
-c core.fsmonitor=false
-c core.hooksPath=/dev/null
-c core.ignoreCase=false
-c core.sparseCheckout=false
-c core.sparseCheckoutCone=false
-c core.untrackedCache=false
-c diff.external=
-c pager.status=false
-c protocol.allow=never
-c protocol.file.allow=never
-c status.submoduleSummary=false
-c submodule.recurse=false
```

Only fixed readiness plumbing/status suffixes are permitted. Before any command
that can compare worktree content, the bootstrap rejects local
include/includeIf, alias, `core.excludesFile`, `core.attributesFile`, any
equivalent exclude/attribute path indirection, executable filter/diff,
fsmonitor, hook, partial-clone/promisor, alternate-object, and
submodule-recursion configuration. User and system configuration is absent
because `HOME` is fresh and `GIT_CONFIG_NOSYSTEM=1`; system attributes are
disabled, and the exact environment inventory admits no exclude, attribute,
object-directory, or alternate-object environment indirection.

Before those commands, the bootstrap opens and holds `info`, `objects`,
`objects/info`, and `objects/pack` one component at a time relative to the held
Git-directory descriptor, using
`O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC`. Relative to the held `info`
descriptor, `exclude` and `attributes` must either be absent or open with
`O_RDONLY|O_NOFOLLOW|O_CLOEXEC` as regular, single-link files owned by the
Git-directory owner and not world-writable.
The bootstrap holds each descriptor and records device, inode, link count,
mode, owner, size, nanosecond mtime/ctime, and SHA-256 before the Git
operations; afterward it rehashes the held descriptor and requires every
field, digest, and descriptor-relative path binding to be unchanged.
Relative to the held `objects/info` descriptor, `alternates` and
`http-alternates` must be absent both before and after the operations.
Relative to the held Git-directory descriptor, `info/grafts` and `shallow`
must also be absent: replacement objects and commit graphs are disabled,
grafted and shallow history are forbidden, and the CP1 ancestry decision uses
the complete object history.

Before any object-reading command, a descriptor-relative metadata walk rooted
at the held `objects` descriptor opens every directory with
`O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC`; every nondirectory entry must be
a regular, single-link, non-world-writable file owned by the Git-directory
owner. It rejects every symlink, special file, unknown loose-object name, and
unknown pack entry before Git can traverse it. The ordered path/type/mode/
owner/device/inode/link-count/size/mtime/ctime metadata snapshot is unchanged
after each object-reading command. A symlink, alternate,
ownership/permission failure, mutation, or path replacement is fatal. Missing
local objects fail and are never fetched. Committed bytes are obtained only by
raw `cat-file` of the exact stage-0 blob OID, never through worktree
conversion, a filter, commit graph, or a path-derived revision expression.

Before the first worktree-inspecting Git subprocess, the bootstrap uses the
already held repository-root descriptor to open every tracked path
component-by-component without reading leaf content. Deduplicated intermediate components require
`O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC`; each leaf requires
`O_RDONLY|O_NOFOLLOW|O_CLOEXEC`, followed by `fstat` proving a regular file.
It fails before that subprocess if it cannot retain all intermediate and leaf
descriptors. The leaf owner-executable bit is set exactly when the stage-0
index mode is `0100755` and clear exactly when that mode is `0100644`; no other
index mode is valid.

For every held directory and leaf, the bootstrap records device, inode, link
count, mode, owner, size, and nanosecond mtime/ctime. Before and after the raw
other-path enumeration, it repeats `fstat` and reopens every path binding
descriptor-relative without following symlinks; every tuple and binding is
identical. Only after raw untracked-path rejection does it stream the raw Git
blob and already-open worktree leaf into byte-count and SHA-256 accumulators.
Before and after each later worktree-inspecting subprocess, it repeats `fstat`,
rehashes every held leaf, and reopens every path binding descriptor-relative
without following symlinks. Every tuple, digest, and directory/leaf binding
remains identical, and blob and worktree size and SHA-256 match exactly. Their
common tuple populates the canonical source record. A persistent or transient
intermediate-component or leaf replacement is therefore fatal and finalizes
no artifact.

Path bytes may be used only for exact Git identity, bytewise ordering,
containment, and file-type checks. Content bytes must not be decoded, searched,
parsed, logged, persisted, archived, extracted, returned to another subsystem,
or used to derive or access a registry, bag, ground-truth, or payload path. Git
cleanliness may inspect tracked bytes only inside the fixed sanitized Git
boundary above and only to produce repository status. No registry/YAML loader,
workspace-local module, provider, executable hook/filter, or network operation
is authorized.

Any missing object, non-stage-0 entry, symlinked path component, nonregular
file, path-binding change, metadata change, size or digest mismatch, source
mutation, or Git-identity mismatch fails the barrier. A source archive
containing the registry may be produced or extracted only after readiness step
8 passes.

After steps 1 through 8 pass, set `prebag_authorized=true`. The runner then
opens the exact worktree registry once through the same descriptor-relative,
component-by-component nonsymlink procedure, reads it into one buffer while
performing the same before/after descriptor and path-binding checks, requires
its size and SHA-256 to equal the post-lock preauthorized source record, and
parses only that same verified buffer. No registry-derived path is resolved,
statted, or opened before equality succeeds. Every later consumer uses that
parsed object rather than reopening the registry.

Any failure terminates owned process groups, closes descriptors, removes
temporary state, leaves no result directory, and retains zero bag-provider
calls. This is the sole preauthorization exception.

## 12. Scope of the offline replay proof

Offline replay is an exact trace-derivation, lifecycle, ordering, serialization,
and live-wiring oracle using the hash/build-ID-anchored shared production
kernels. It is not an independent proof of Schur reduction, gate, preview, or
retraction mathematics. CP1 and CP2-A/B independent analytical and numerical
oracles remain the authority for those equations. CP2-C proves that the audited
production mathematics is connected to the recorded live baseline and shadow
paths exactly as contracted.

## 13. Approval and artifact binding

First commit this reviewed document and its pending gate pointer without an
approval record. Explicit approval names that exact reviewed commit. A later,
separate commit creates `project/cp2_c_clarification_approval.json` with exact
keys:

`schema_version`, `record_type`=`cp2_c_clarification_approval`, `reviewer`,
`approved_utc`, `reviewed_commit`, `document_path`, `document_git_blob`,
`document_sha256`, `approval_statement`, and `exceptions`.

`reviewer` is `Moksh Trehan`; `document_path` is this path; the blob and
ordinary SHA-256 bind its bytes at `reviewed_commit`; `approval_statement`
retains the exact user statement; and `exceptions` is an empty array. The
approval-record commit changes the gate status to `approved` and records the
reviewed commit and approval-record path. It does not edit this reviewed
document.

Every later unit or recorded artifact treats this document and the approval
record as governing contracts. Both paths and hashes are included in its
contract inventory, source-input inventory, retained source archive roots, and
detached verifier. A missing path, wrong Git blob, wrong byte SHA-256, nonempty
exception list, or source/archive/verifier omission fails provenance before
recorded-data access.

## 14. Mandatory protecting tests before CP2-C readiness

In addition to all existing tests, the following cases are mandatory and
fail-closed:

- shuffled unordered-map insertion and changed process addresses produce the
  same canonical bytes;
- one-at-a-time mutation of every composite coefficient, identity, metadata,
  key, shape, and pointer association is detected;
- nonzero orientation and vector increments for IMU, clone, and landmark
  detached objects match the actual live-update bytes;
- global sentinel and every anchored-representation identity rule is tested;
- phase populations `[0,1]` and `[0,1,2,3]`, order, truncation, duplicate,
  reserved-byte, and postcommit-failure cases are tested;
- phase-0 projection performs no second State read; phase 2 precedes final
  phase-1 verification; unpublished phase-2 bytes are discarded on every
  suppressed commit; and the timing endpoint is the first postcommit operation;
- allocation-failure injection proves phase-3 fill and handoff are
  allocation-free/nonthrowing and sink failure sets the fatal latch;
- the exact quaternion squared-norm bound passes at equality and fails at the
  next larger representable error for every active and fixed quaternion role;
- for every composite role, positive/negative infinity and representative
  quiet/signaling NaN codec inputs round-trip losslessly at unit level;
  tentative phase-0 nonfinite input takes the run-fatal no-artifact path before
  raw assembly/counting, changed phase-1 input takes `snapshot_mismatch`, and
  phase-2 input prevents commit; a complete nonfinite phase 3 preserves
  `committed_counted` and fails; and no JSON nonfinite token or string
  substitute is emitted;
- every counter unit, zero/nonzero denominator, checked-add/product overflow,
  exact agreement cross-product, and gamma referential-copy rule is tested;
- `opaque_source_digest_scope`,
  `preauthorization_registry_semantic_access`, and
  `tracked_blob_worktree_mismatch` protect the registry boundary; and
- a synthetic temporary Git repository with a decoy registry tests symlink
  component swaps, in-place mutation, path replacement, `.git` symlink and
  directory replacement, gitfile rejection, external `commondir`,
  `core.worktree`, true bare/worktree configuration, `config` mutation,
  `config.worktree`, grafts, shallow history, replacement objects, commit
  graphs, split indexes, gitlinks, intermediate `info`/`objects` symlinks,
  loose/pack-object symlinks, Git-2.25 descriptor-path canonicalization with
  transient-swap detection, `core.filemode=false`, executable-bit mismatch,
  `core.ignoreCase=true` with tracked `foo` plus untracked `Foo` and a
  directory-component collision on a case-sensitive filesystem,
  sparse-checkout configuration/file/index flags, non-object administrative
  symlinks/hardlinks/mutations, HEAD/ref/packed-refs indirection, and
  world-writable Git-info files, plus status blocked before opening a decoy
  behind a tracked intermediate symlink, transient tracked-path swaps, and a
  parser spy proving raw untracked enumeration never opens an untracked nested
  repository `.git` file or `.git/HEAD`,
  local/global exclude and attribute path indirections, untracked
  `.gitignore`/`.gitattributes`, Git-info symlinks and mutations, object and
  HTTP alternates, fsmonitor/hook/filter configuration,
  partial-clone/lazy-fetch/network attempts, untracked rejection before
  content-open, and parser/provider-call spies without semantically opening the
  real registry; and
- every one of the five preregistered entry points passes its complete
  exclusive self-test with zero bag-provider calls before any recorded input
  is authorized.

No CP2-C evidence is credited by these unit tests. CP2-C remains pending until
the full recorded campaign, independent offline replay, and detached verifier
all pass at one clean exact commit and tree.
