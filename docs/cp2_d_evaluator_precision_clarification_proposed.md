# Proposed CP2-D evaluator-precision clarification

Status: **proposed, not approved, and non-authorizing**

Recorded-input access used for this finding: **none**

Date: 2026-08-03

## Conflict discovered before execution

The frozen CP2-D contract requires both of the following:

1. the retained `evo_ape` command has exactly
   `evo_ape tum GT_SHARED.tum MODE_SHARED_ALIGNED.tum -r trans_part
   --t_max_diff 0.01`, with no result archive; and
2. the RMSE parsed from the command's console output agrees with the direct
   binary64 recomputation within `1e-12 + 1e-10*abs(reference)`.

The pinned installed evaluator is evo 1.31.1. Its
`evo.core.result.Result.pretty_str()` formats every console statistic with
exactly six digits after the decimal point (`{:.6f}`). For example, an
internal value `0.12345678901234567` is printed as `0.123457`. Therefore the
console representation has an ordinary rounding error as large as
`0.5e-6`, many orders of magnitude above the frozen comparison tolerance.
Passing all six evaluator joins across three sequences would be accidental,
not a sound mathematical gate.

No CP2-D bag, ground-truth file, registry entry, or trajectory was opened to
discover or confirm this conflict. It follows from the frozen contract and
the pinned evaluator implementation alone.

## Evaluator execution and provenance blocker

The currently installed evaluator does not satisfy the frozen execution
surface either. The observed command is
`/home/moksh/.local/bin/evo_ape`, reporting evo version `1.31.1`, while the
frozen child environment's exact `PATH` does not include
`/home/moksh/.local/bin`. The literal `evo_ape` argv therefore cannot resolve
to that installation. Merely adding the user-local directory to `PATH` would
not be an acceptable correction: the installed wrapper is group-writable and
is not an immutable, privately controlled executable input.

The current host record also permits `evo_version=null`. That nullability can
turn a missing, unresolvable, or unproven evaluator into retained host
metadata instead of a pre-data failure. A version string alone would still be
insufficient because it does not bind the console-script wrapper, Python
interpreter, imported entry-point module, installed distribution files, or
dependency closure that actually computes the result.

These observations used only command-location, file-mode, version, and frozen
environment metadata. No dataset registry, bag, ground truth, or trajectory
was inspected.

## Required disposition

The current runner and verifier intentionally retain the contradictory frozen
precision check and have no authority to substitute a different command. The
excluded `PATH` entry prevents the observed wrapper from resolving, but the
nullable host-version field is not a sufficient fail-closed provenance gate.
CP2-D actual execution must not start under either surface. The evaluator must
not be discovered from the ambient `PATH`, user site packages, or a nullable
host-version field. Because the corrections change tracked
runner/schema/verifier bytes, CP2-C recorded execution should also wait:
otherwise the correction would invalidate its exact source/unit anchor and
force CP2-C to be repeated.

## Direct-math numerical dependency blocker

The independent CP2-D calculation is not yet dependency-bound either.
`scripts/cp2/cp2_sequence_math.py` describes its SVD and matrix-product order
as NumPy 1.24 semantics, but the frozen isolated interpreter
`/usr/bin/python3 -I -B` currently imports system NumPy 1.17.4 from
`/usr/lib/python3/dist-packages`, backed by the host BLAS/LAPACK libraries.
The current host/provenance schema retains neither a mandatory NumPy identity
nor the Python extension, BLAS, LAPACK, and loader closure that actually
executes Kabsch SVD and matrix products. A source hash for
`cp2_sequence_math.py` therefore does not bind the numerical implementation.

This mismatch was found using only interpreter/package identity and numerical
configuration metadata. No registry, bag, ground truth, or trajectory was
opened. CP2-D remains blocked even if the console-precision conflict alone is
fixed: silently relabeling 1.17.4 as 1.24, accepting an ambient package, or
assuming cross-version/BLAS bit identity is forbidden.

The review-candidate runner therefore returns failure after exact CLI
validation but before loading readiness or opening any recorded input. The
detached CP2-D verifier likewise validates only its caller-supplied absolute
path syntax and manifest-digest syntax, then fails before artifact access.
Removing either pre-access block requires the approval-bound implementation
and dependency identities below.

## Recommended replacement contract

Subject to explicit approval, replace the evaluator-result transport and bind
the evaluator execution surface as follows. All associations, common
alignment bytes, direct-RMSE arithmetic, limits, mode order, and other CP2-D
requirements remain unchanged.

1. Each mode's exact evaluator argv has exactly eleven elements:
   `${CAPSULE_ROOT}/APPROVED_LAUNCHER tum GT_SHARED.tum
   MODE_SHARED_ALIGNED.tum -r trans_part --t_max_diff 0.01 --save_results
   ABS_MODE_RESULTS.zip --no_warnings`. The result path is a distinct
   normalized nonsymlink path inside the hidden sequence partial. The first
   element is the absolute path inside the privately staged capsule; literal
   or PATH-resolved `evo_ape` is forbidden. `-a`, `--align`, scale correction,
   and any second association remain forbidden.
2. Retain the two result ZIP files as role `evaluator` artifact members and
   bind their size and SHA-256 in the command record, provenance inventory,
   manifest, and detached verifier.
3. Parse the ZIP files without executing or loading any archived object. The
   verifier must reject duplicate names, links, unsafe names, encryption,
   unsupported compression, excessive member/count/total sizes, trailing or
   overlapping records, and any malformed JSON. It reads only `stats.json`.
4. `stats.json` must contain evo's exact seven finite statistics
   `max`, `mean`, `median`, `min`, `rmse`, `sse`, and `std`, with no duplicate,
   missing, or extra key. Its full-precision `rmse` becomes the retained evo
   value.
5. The full-precision archive RMSE must agree with the independently computed
   direct shared-population RMSE within the existing
   `1e-12 + 1e-10*abs(direct_shared_population_RMSE)` tolerance. The direct
   value, not the archive value, is the reference in the relative term.
6. The six-decimal console row remains retained as diagnostic evidence. It
   must equal the result of formatting the archive RMSE with evo 1.31.1's
   exact `{:.6f}` rule; it is no longer compared directly to the unrounded
   RMSE.
7. Add protecting corruptions for a rounded-console/direct mismatch, archive
   RMSE drift, missing/duplicate/extra `stats.json` key, duplicate or unsafe
   ZIP member, archive hash drift, wrong result path, forbidden alignment
   option, and a no-op result archive.

This replacement retains the independent direct calculation while preserving
evo's full-precision result, rather than weakening the mathematical tolerance
to accommodate a presentation-only string.

### Private relocatable evaluator capsule

The approved implementation should use one evaluator profile only: a
relocatable, privately staged capsule. Direct use of
`/home/moksh/.local/bin/evo_ape`, any other ambient `PATH` candidate, or a
mixed capsule/host fallback is forbidden.

8. Before any semantic registry access or dataset-provider call, construct a
   fresh capsule root under the readiness-owned temporary root. The root and
   every directory are owned by the recording uid, mode `0700`, nonsymlink,
   and single-link where applicable. Every regular file is opened without
   following links, checked before and after hashing/copying, and is neither
   group- nor other-writable. Any identity, mode, link-count, size, or digest
   change fails before data authorization and removes the partial capsule.
9. Stage the capsule only from an explicitly approved, locally retained input
   archive or installation-artifact set. Freeze a canonical root-relative
   inventory containing every launcher, interpreter, standard-library file,
   native extension, evo module, `evo` distribution-metadata file, dependency
   file, license/notice file, relative path, role, mode, size, and SHA-256.
   Reject an omitted, duplicate, extra, absolute, parent-traversing, linked,
   device, socket, or writable member. The canonical inventory and capsule
   archive are evaluator-role artifacts whose hashes are bound by provenance,
   the manifest, and the detached verifier.
10. The capsule launcher must resolve its interpreter and entry point relative
    to the capsule root, not through a shebang using `/usr/bin/env`, `PATH`,
    the user site, or host package discovery. The command is executed through
    the already verified capsule path or held descriptor. Provenance binds the
    exact launcher bytes, interpreter bytes and version, entry-point module
    and callable, distribution name `evo`, distribution version `1.31.1`,
    complete distribution/dependency inventory digest, and native-library
    closure. Relocating the identical capsule changes no canonical inventory
    byte or semantic binding.
11. Replace the nullable host field with mandatory evaluator identity. At a
    minimum retain exact nonnull `evo_version="1.31.1"`, evaluator-profile,
    capsule-manifest, launcher, interpreter, entry-point,
    distribution-inventory, dependency-closure, sanitized-environment, and
    preflight-record SHA-256 fields. Absence, null, wrong type, version drift,
    path drift, or disagreement between metadata and the executed bytes is a
    pre-data failure. A host-level `evo_ape` observation may be retained only
    as non-authoritative diagnostic metadata and is never an execution input.
12. Use an exact allowlisted child environment. Invoke the capsule without
    evaluator lookup through `PATH`; set a private `HOME`, `TMPDIR`, XDG cache
    and configuration roots, and plotting/font cache roots inside the
    readiness temporary root; fix locale, timezone, hash seed, and numerical
    thread counts; disable Python user-site loading; and reject or clear
    `PYTHONPATH`, unintended `PYTHONHOME`, `LD_PRELOAD`, `LD_LIBRARY_PATH`,
    virtual-environment selectors, Python startup hooks, and other package or
    loader injection controls. Record the sorted environment and its canonical
    SHA-256 for every preflight and evaluator command.
13. Complete a dataset-free preflight before the authorization barrier
    releases registry, bag, ground-truth, or trajectory access. It revalidates
    the held capsule identity, obtains version `1.31.1` from the bound
    distribution and the exact evaluator version command, and runs the exact
    evaluator path on a generated synthetic TUM fixture with predetermined
    association, archive statistics, console formatting, and exit status.
    The preflight uses the same launcher, interpreter, module closure,
    environment, result-ZIP parser, and command-recording path as CP2-D. Any
    mismatch leaves zero dataset-provider calls and no result directory.
14. Immediately before and after each real evaluator invocation, recheck the
    held launcher/interpreter/capsule identities and require the exact approved
    argv, environment digest, working-directory role, exit status, version,
    result path, and output hashes. A replacement, mutation, fallback,
    unexpected import root, or environment drift terminates the owned process
    group and fails the sequence without publishing partial evidence.
15. Detached verification must use the retained canonical capsule inventory
    and bytes, never a verifier-host `evo_ape`. It independently rehashes and
    validates archive paths, modes, identities, launcher/interpreter/module and
    distribution bindings, dependency/native-library closure, version and
    environment records, preflight result, and all six real command joins. It
    rejects a report whose host record has a null evo version even if all
    numeric evaluator fields appear to pass.
16. Add protecting cases for missing capsule execution permission, ambient
    `PATH` shadowing, host-evaluator fallback, group/other-writable capsule
    input, symlink/hardlink/path replacement, launcher/interpreter/module or
    distribution drift, null/wrong evo version, dependency omission,
    `PYTHONPATH`/user-site/loader injection, post-preflight mutation,
    relocation to two distinct private roots, preflight no-op, detached use of
    a host evaluator, and coordinated metadata/hash substitution.

17. The reviewed implementation must also freeze the direct-math numerical
    stack. Bind the exact isolated Python interpreter, NumPy distribution
    version and complete file inventory, imported `_multiarray_umath` and
    linear-algebra extension bytes, and their ELF/BLAS/LAPACK/Fortran runtime
    closure. The direct verifier must load only this approved stack from held
    capsule descriptors or an equivalently immutable private capsule; ambient
    system/user packages and a version-only claim are forbidden. Record the
    exact thread-control environment and require one approved thread policy.
18. Add a dataset-free direct-math preflight with reviewed binary64 known
    answers for nontrivial full-rank Kabsch SVD, determinant correction,
    alignment reuse, matrix products, ordered RMSE accumulation, and the p95
    interpolation boundary. Retain exact input/output payload hashes and
    binary64 bits. Run it through the identical interpreter/module/native
    closure used for artifact verification, and repeat it in detached mode.
    Version, native-library, thread, known-answer, or loader drift is a pre-data
    failure.

The exact evaluator and direct-math capsule construction format, canonical
inventory codec, expected version-command bytes, interpreter version, NumPy
version, dependency list, file hashes, numerical/native-library closure,
thread environment, and known-answer payloads must be recorded in the reviewed
implementation commit before approval. This proposal does not authorize
deriving those values from the mutable host during a recorded run.

## Data-free implementation candidate

The source tree now contains four non-authorizing, data-free candidate
primitives. They are not imported by either public actual-mode path and do not
remove either pre-access block:

- `scripts/cp2/cp2_capsule.py` defines an uncompressed, length-prefixed
  `.cp2cap` regular-file-only stream, a canonical relocation-independent
  inventory, a strict profile schema, and a Linux-only private stager. The
  stager pins directories and the input archive through descriptors, streams
  the already hash-bound bytes through `O_NOFOLLOW|O_EXCL` members, validates
  the complete tree, stages under an unpredictable hidden name, and publishes
  with `renameat2(RENAME_NOREPLACE)`. Failure cleanup also pins and revalidates
  every descendant before removal, refuses links, multiple links, ownership or
  mode drift and device crossings, and rejects directory/file substitution
  without deleting the substituted bytes. Unsupported descriptor capabilities
  fail closed. Separate evaluator and direct-math profiles are mandatory.
- `scripts/cp2/cp2_evo_result.py` manually accounts for every classic-ZIP
  local, central-directory, and EOCD byte. It accepts only an exact Unix ZIP
  2.0 regular-file surface, STORE or version-consistent raw DEFLATE, no extra
  fields/comments/preamble/gaps/overlap/trailing bytes/ZIP64/encryption/data
  descriptors, and decompresses only root `stats.json` under fixed bounds.
  The exact seven finite nonnegative, non-negative-zero statistics and basic
  min/mean/median/RMSE/max ordering are checked, including the mathematically
  necessary exact `RMSE >= mean` relation for nonnegative errors. Console RMSE
  is diagnostic six-decimal formatting; the archive RMSE is the full-precision
  value.
- `scripts/cp2/cp2_f64_codec.py` defines canonical finite-binary64 array IPC.
  Values are lower-case 16-hex-digit big-endian IEEE-754 bit strings; shape is
  checked u64 arithmetic; JSON has exact keys/order/spacing and one LF; signed
  zero is preserved; nonfinite values, alternate JSON spellings, duplicate
  keys, excess rank/elements/document bytes, and overflow fail closed.
- `scripts/cp2/cp2_direct_kat.py` defines the exact five-case direct-math
  expectation/response bundle. It freezes every proposal input row and every
  case/input/output name, shape and order as binary64 bits; requires exactly
  two complete repeats carrying the same input projection; compares every
  retained finite-binary64 bit string to the reviewed expectation; requires
  the common alignment bytes to equal case 1 byte-for-byte; and directly
  freezes the already specified ordered-RMSE and two p95 answer bits. It
  deliberately does not derive the stack-specific SVD/Kabsch/matrix-product
  bits.

The candidate capsule profile has exact, nonnull fields for the clarification
commit; target Linux ABI and CPU-dispatch policy; archive and complete file
inventory; one launcher/interpreter and a source-linked entry module;
kind-specific evaluator or direct-math roles; distribution member subsets
whose inventory hashes are recomputed; every native consumer's ELF type,
linkage kind, interpreter, ordered RPATH/RUNPATH, SONAME and ordered
`DT_NEEDED` names; a complete loader/library provider graph with search-path
reachability; every license/notice; an exact private single-thread
environment; exact execution, version, and preflight commands; injection
denylist; floating-point rounding/subnormal/control-state identity; retained
preflight fixtures and known-answer bytes; and a canonical full-profile
SHA-256. The x86 candidate requires MXCSR `0x1f80` and x87 control word
`0x027f` (round-to-nearest, 53-bit significand, no FTZ/DAZ); the AArch64
candidate requires zero FPCR/FPSR under the stated mask. The evaluator command
template is the eleven-element command above. The direct-math command template
is a five-element absolute launcher request/response command. A retained
known-answer digest must equal the selected capsule member's actual SHA-256;
an unrelated opaque digest is rejected.

No real capsule archive or `project` profile has been created. The synthetic
tests use invented bytes only; passing them proves parser/stager logic, not an
evo, CPython, NumPy, BLAS, LAPACK, libc, libm, or CPU identity. “Relocatable”
at this stage means transport bytes and canonical inventory are root-neutral.
Executable relocation and identical numerical answers at two roots remain an
approved-capsule preflight requirement. The evidenced data-free inventory is
78 tests: 26 capsule/profile/stager cases, 29 ZIP/statistics cases, 12
binary64-codec cases, and 11 direct-KAT bundle cases.

## Direct-math known-answer plan

The selected numerical capsule must run the following ordered five cases
twice in one process and reproduce the retained bit records exactly; detached
verification repeats them with the same retained capsule. Expected SVD/Kabsch
bits are deliberately not populated from the ambient host.

1. `kabsch_proper_full_rank` uses source rows
   `(3,0,0),(-3,0,0),(0,2,0),(0,-2,0),(0,0,1),(0,0,-1)` and target rows
   `(1,1,.5),(1,-5,.5),(-1,-2,.5),(3,-2,.5),(1,-2,1.5),(1,-2,-.5)`.
   The mathematical transform is `Rz(+90 degrees)`, translation `(1,-2,.5)`.
   Retain rotation, translation, singular values, rank threshold, determinant,
   and orthogonality-error bits from the selected approved stack.
2. `kabsch_reflection_correction` uses the same source and target rows
   `(-4,2,-.5),(2,2,-.5),(-1,4,-.5),(-1,0,-.5),(-1,2,.5),(-1,2,-1.5)`.
   The unconstrained reflection is `diag(-1,1,1)`; the production correction
   must return a proper rotation (mathematically `diag(-1,1,-1)`).
3. `common_alignment_matrix_products` reuses case 1's alignment byte-for-byte
   for both modes. The Schur source adds dyadic delta `(.125,-.25,.5)` and uses
   stored quaternion `(0,0,.6,.8)`. Retain both aligned position/rotation
   populations and one explicit 3x3 matmul rounding discriminator. A one-ULP
   candidate-alignment change must reject.
4. `ordered_translation_rmse` uses aligned rows
   `(2^27,1,1),(1,1,1),(1,1,1)` against zero ground truth. The frozen ordered
   result bits are `419279a74590331c`; forbidden small-first accumulation gives
   `419279a74590331d`.
5. `linear_p95_boundary` uses input bits
   `bff539d94973bf31,3ff1c926addad2ec` and q bits `3fee666666666666`.
   Frozen-order output is `3fefab9a2960fd9e`; the forbidden rearrangement is
   `3fefab9a2960fda1`. The unsorted `[9,1,7,3,5]` companion result is
   `4021333333333333`.

Every binary64 is encoded as exactly 16 lower-case hexadecimal digits in the
canonical codec; JSON floats are forbidden. The native closure must include
the dynamic loader, CPython/NumPy extensions, BLAS, LAPACK, Fortran runtime,
libc, **libm** (production uses `sqrt`, `acos`, and `pi`), and every other
mapped dependency. Environment strings alone are not proof of one-thread or
floating-point state: preflight must report and verify actual backend thread
count, CPU dispatch/core policy, FE_TONEAREST, and x86 MXCSR or AArch64 FPCR
state with subnormal preservation.

## Decisions still required before a profile can exist

Moksh Trehan must explicitly select and approve all of the following before
real capsule bytes may be inspected, built, or bound:

1. the CP2-D execution target: desktop `x86_64` or Jetson `aarch64`. The
   current CP2 proposal is desktop evidence and Jetson remains CP6 unless that
   checkpoint architecture is explicitly changed. One capsule cannot span
   the two ABIs;
2. the exact locally retained evaluator and direct-math artifact sets, or
   explicit authority to build them from named audited sources;
3. exact CPython, NumPy, evo 1.31.1, numerical backend, libc/loader, complete
   native closure, CPU-feature/dispatch and fixed-core policy, and all
   corresponding-source/license/notice bytes;
4. the single-thread policy and an observed backend-thread-count verifier;
5. the exact version/preflight outputs, evo ZIP profile, and stack-specific
   Kabsch/SVD/matmul expected bits; and
6. the hash-seed disposition. `/usr/bin/python3 -I` implies `-E`, so recording
   `PYTHONHASHSEED=0` while using `-I` would be false: the interpreter ignores
   that environment setting. The recommended options are a tiny bound launcher
   using `PyConfig` isolated mode with a fixed hash seed, or explicit approval
   and proof that the capsule protocol is hash-order-independent.

After those choices, the exact profile/capsule implementation must be
committed for review and approved. A separate source-binding commit, fresh
exact-HEAD unit anchor, and readiness pass then precede any CP2-D data access.

## Approval boundary

This document does not itself change the frozen contract. Implementation and
recorded execution require Moksh Trehan's explicit approval of the exact
committed version of this clarification. Until then, CP2-D remains
`blocked_pending_evaluator_precision_clarification`.
