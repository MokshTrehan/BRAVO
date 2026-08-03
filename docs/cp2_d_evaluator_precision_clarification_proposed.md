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

1. Each mode's exact evaluator argv appends
   `--save_results ABS_MODE_RESULTS.zip --no_warnings`, where the result path
   is a distinct normalized nonsymlink path inside the hidden sequence
   partial. `-a`, `--align`, scale correction, and any second association
   remain forbidden.
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
   `1e-12 + 1e-10*abs(reference)` tolerance.
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

## Approval boundary

This document does not itself change the frozen contract. Implementation and
recorded execution require Moksh Trehan's explicit approval of the exact
committed version of this clarification. Until then, CP2-D remains
`blocked_pending_evaluator_precision_clarification`.
