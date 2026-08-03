# Proposed CP2-E fixed-clock and exact-timing clarification

Status: **proposed, not approved, and non-authorizing**

Recorded-input access used for this finding: **none**

Clock-control access or mutation used for this finding: **none**

Date: 2026-08-03

## Mathematical and evidence gaps found before execution

CP2-E is correctly blocked before artifact, registry, or bag access, but its
current preregistration is not yet precise enough to authorize evidence. Three
data-free defects must be replaced before a profile can exist:

1. the synthetic runner and independent-verifier oracles convert integer
   nanoseconds to binary64 when computing linear p50 and p95. Integer
   nanoseconds above `2^53` are not all exactly representable, so a retained
   percentile or boundary decision can change solely because of conversion;
2. the ratio gates use binary64 division and decimal literals `1.10` and
   `1.15`. The frozen limits are the exact rational numbers `11/10` and
   `23/20`; division and rounded literals are unnecessary and make a boundary
   decision representation-dependent; and
3. the current common-population helper checks only that every retained item
   occurs on both sides. It does not prove that the retained population equals
   the complete intersection, so a mutually present but inconvenient sample
   could be omitted.

The committed artifact section is explicitly only a preregistration, and no
`project/cp2_timing_profile.yaml` exists. Therefore these findings do not
invalidate timing evidence: no eligible CP2-E evidence has been run.

## Exact timing mathematics

Subject to explicit approval, replace all timing-quantile and ratio decisions
with the following integer/rational contract. No binary64 conversion is
permitted in this decision path.

For sorted integer-nanosecond samples `x[0] <= ... <= x[n-1]` and reduced
quantile `q=a/b`, define

```text
k = (n - 1) * a
lo = floor(k / b)
r = k mod b
Q_q = x[lo] + r * (x[lo + 1] - x[lo]) / b,  when r != 0
Q_q = x[lo],                                  when r == 0
```

with `q=1/2` for p50 and `q=19/20` for p95. This is exactly the frozen NumPy
linear-interpolation definition; it is not nearest-rank selection. Inputs,
counts, ranks, differences, and resource limits are checked before use.

Every retained exact value is a reduced nonnegative rational encoded with
exact keys `numerator_hex` and `denominator_hex`. Each is exactly 32 lower-case
hexadecimal digits (unsigned 128-bit, big-endian lexical form), the
denominator is nonzero, `gcd(numerator,denominator)=1`, and zero is encoded only
as `0/1`. The approved sample-count cap and u64 input domain must prove that
every generated quantile and ratio fits this u128 representation; overflow is
a pre-evidence failure, never a float fallback.

For a candidate quantile `C=c_n/c_d`, baseline quantile `B=b_n/b_d`, and
positive `B`, the gate `C/B <= L_n/L_d` is decided only by

```text
c_n * b_d * L_d <= L_n * c_d * b_n.
```

Use `(L_n,L_d)=(11,10)` for p50 and `(23,20)` for p95. Products are exact and
checked against the frozen u128 bound before comparison. The report retains
both mode quantiles, the reduced candidate/baseline ratio, both cross products,
and the Boolean result. Equality passes; a one-unit increase of the left cross
product fails. The median of the three paired ratios is the middle ratio under
exact cross-multiplication ordering, not a binary64 median. Ordering two
retained u128 ratios uses checked u256 cross products (or an exactly
equivalent cross-cancelled comparison); it must not impose an unsound u128
intermediate limit.

## Data-free implementation candidate

`scripts/cp2/cp2_timing_math.py` now implements only the non-authorizing
integer/rational layer described above. Its narrow API computes checked exact
linear ranks, p50/p95 from one retained sorted u64 population, reduced
candidate/baseline ratios, exact median-of-three ordering, and inclusive
`11/10` and `23/20` gates. Each p50/p95 composite retains the complete sorted
population and a rederived domain-separated SHA-256, so independently forged
quantiles cannot be combined as though they came from one population. Nested
dataclass evidence is reconstructed and revalidated rather than trusted by
type alone.

The candidate creates no artifact codec or profile and performs no file,
clock, host-control, registry, bag, build, or result access. Its 37-case
isolated synthetic suite passed, and a separate audit compared 49,600 cases
against Python's independent exact `Fraction` oracle with no disagreement.
The module and test SHA-256 values before integration were respectively
`4f9a394f808d69a5846b6b9d070fe98ed113f329306c681f7207c2a1d6a6ee34` and
`e321a68dcd5ba495923975b02a92024c2801e941b8b31d5e2fff967f0f1dc960`.

The still-blocked public timing runner and independent verifier now use
separate stdlib-only exact rational corruption oracles: their linear
interpolation and ratio decisions contain no binary64 conversion, and their
common-population checks require equality with the complete intersection.
The timing runner has 43 synthetic cases and the union verifier has 83,
including omitted-bilateral, u64-boundary, and above-`2^53` mutations. These
changes do not enable either actual path.

## Exact common-population contract

For each paired run, first select rows that are all of: unique by camera-header
timestamp, primary, nonempty, preflight-accepted, committed, and at or after
the inclusive integer warm-up boundary. The retained common timestamp list
must equal, byte-for-byte and in increasing u64 order, the complete set
intersection of the two selected timestamp sets. Subset-only, superset,
duplicate, unilateral, reordered, or omitted-bilateral populations reject.

Every common timestamp must join exactly one serial-pair record and one timing
row in each mode. Each joined `cam0_record_time_ns` independently satisfies
the warm-up boundary. Durations are the exact retained u64 nanoseconds from
the frozen internal timer endpoints; negative, Boolean, floating, duplicate,
missing, or nonprimary values reject.

The canonical common payload is the already preregistered domain
`SchurVIO-CP2-timing-common-v1\0`, followed by timing-pair index u64, count
u64, and increasing camera-header timestamps as u64. The payload bytes and
SHA-256 are both retained and rederived.

## Proposed fixed-clock profile boundary

The future `project/cp2_timing_profile.yaml` must be strict canonical JSON
bytes despite its historical filename (JSON is a YAML-compatible subset). It
has one UTF-8 encoding, sorted keys, compact separators, one terminal LF, no
duplicate keys, no floats, no aliases/tags/comments, and no unknown fields.
Its SHA-256 binds the exact bytes. At minimum it must freeze:

- schema/profile/checkpoint identity and the exact approved clarification and
  source-binding commits;
- target class, operating system, machine architecture, immutable hardware
  identity, kernel/OS identity policy, and whether the evidence target is the
  frozen desktop or an explicitly approved replacement;
- exact sequence index/ID, frozen offset, approved bag SHA-256 and warm-up
  duration `60000000000` ns;
- pair order exactly `nullspace/schur`, `schur/nullspace`,
  `nullspace/schur`, with six fresh-process run slots;
- exact executable, DSO closure, runtime configuration, launch, pair-index,
  unit-anchor, readiness-contract, and source identities required to match
  across all six runs;
- sorted unique CPU IDs, process and every-thread affinity, online state,
  scaling driver, governor, min/max/current frequency, boost/turbo state,
  and any driver-specific fixed-clock fields;
- exact absolute sampling-command argv, environment, cwd, timeout, maximum
  output size, parser, expected exit status, and expected values for every
  control. Shell execution and ambient `PATH` resolution are forbidden;
- pre-run and post-run snapshot cadence, equality rules, throttling/thermal
  rejection fields, and an explicit `validate_only` control policy; and
- sample/file/count/JSONL/output/process/time limits, exact quantiles `1/2`
  and `19/20`, gates `11/10` and `23/20`, the common-payload domain, and all
  artifact schema/version identifiers.

The runner may observe the approved controls but must never write a sysfs,
procfs, MSR, governor, affinity, boost, turbo, or frequency control. The host
must already be configured outside the evidence runner under separate user
authority. Missing commands, unreadable controls, unexpected values,
`ondemand`, nonfixed min/max/current values, drift, thermal throttling, extra
CPUs/threads, or affinity escape fail before or invalidate timing evidence;
there is no automatic repair or retry with changed controls.

## Proposed complete artifact boundary

The authorizing replacement must freeze exact schemas for every file, not only
the report sketch. The artifact remains a hidden partial until complete
verification and contains exactly:

- canonical `cp2_report.json` and `provenance.json`;
- canonical `commands.jsonl` and `timing_samples.jsonl`;
- six run records and their exact serial/callback/updater/timing traces;
- six pre-run and six post-run clock/affinity/thread snapshots;
- three canonical common-population payloads;
- three pair-result records with exact rational quantiles, ratios, gate cross
  products, and pass flags; and
- canonical `SHA256SUMS` covering the exact closed inventory.

Provenance binds the approved profile bytes, approval record, exact clean
commit/tree/index, fresh unit artifact and external anchor, readiness result,
runtime executable and complete DSO/config/launch identities, input identity,
host/boot identity, command environments, and all clock snapshots. Commands
have exact contiguous IDs and bind argv, environment, cwd, start/end state,
process group, exit status, bounded stdout/stderr, and output hashes. Every
JSON/JSONL file uses strict duplicate-key rejection, exact key inventories,
finite integer domains, bounded line/file counts, and one canonical spelling.

The campaign report passes only if all three p50 gates and all three p95 gates
pass, every population/provenance/clock/affinity/thread invariant holds, the
exact median-of-three ratios are rederived, all six runs complete, and every
artifact byte is closed by the manifest. No aggregate can hide a failed pair.

## Data-free protecting-test minimum

Before actual mode is enabled, synthetic tests must distinguish linear
interpolation from nearest-rank selection and cover:

- values below, at, and above `2^53`; unsorted input; singleton, even, and odd
  populations; p50/p95 integral and fractional ranks; zero and u64-maximum
  endpoints; count/rank/product/resource overflow; and alternate rational
  encodings;
- exact equality at `11/10` and `23/20`, a one-cross-product-unit failure,
  zero baseline, cross-cancelled fractions, ratio ordering, and the exact
  median of three;
- omitted bilateral, unilateral, extra, duplicate, reordered, non-u64, and
  empty common populations plus inclusive warm-up addition overflow;
- profile duplicate/missing/extra keys, noncanonical bytes, target/profile/
  source/approval drift, wrong pair order, mutable/relative command paths,
  shell use, environment injection, CPU duplication, nonfixed clocks,
  `ondemand`, boost/turbo, affinity/thread escape, snapshot drift, bounds, and
  any attempted control mutation; and
- artifact/file omission or substitution, six-run identity drift, incomplete
  joins, timer-endpoint mismatch, noninteger durations, ratio/quantile/common
  payload mutation, manifest corruption, and both public preprofile paths
  remaining blocked before caller-supplied artifact, registry, bag, or clock
  access.

## Decisions and approvals still required

No real timing profile may be created from guesses. Moksh Trehan must review
and explicitly decide:

1. whether CP2-E remains the frozen fixed-clock **desktop** checkpoint, with
   Jetson Nano target evidence deferred to CP6, or whether the checkpoint
   architecture itself is replaced. A Jetson is not silently relabelled as a
   desktop;
2. the exact machine, CPU IDs, affinity, driver, governor, frequencies,
   boost/turbo state, sampling commands, thermal/throttling policy, and host
   configuration procedure;
3. the exact sequence/input identity and all resource bounds;
4. permission for a later fresh process to read the approved clock/control
   surfaces and recorded input after all prior gates pass; this proposal gives
   no permission to change a host control; and
5. the exact committed profile/schema implementation and its separate
   approval-binding record.

CP2-E also remains serialized behind admissible CP2-C and passing CP2-D.
Pending CP2-C incident disposition/replacement approval and pending CP2-D
capsule/precision approval cannot be bypassed by completing this data-free
work.

## Approval boundary

This document and its synthetic primitives do not alter the frozen contract,
authorize a timing profile, authorize recorded-input access, authorize clock
observation or mutation, or claim a CP2-E result. Until a reviewed exact
replacement and profile are explicitly approved and source-bound, the public
runner and detached verifier must continue to fail before those accesses.
