# CP2 recorded-evidence contract

Status: **frozen evidence mathematics and thresholds; campaign evidence pending**

Parent contract: `docs/cp2_one_pass_contract.md`

Machine artifact schema: `docs/cp2_artifact_schema.md`

Parent-contract source commit: `43c9b6339c154f97bc2538bb3619a7a1e8503a25`

Parent-contract SHA-256: `e78ba5b5c87264c3d04b24069f49adee2e2c02433058717a02be450d620d5f6b`

This addendum resolves evidence-population and serialization details that the
parent CP2 contract left implicit. It does not relax a threshold, authorize a
new estimator mode, or change the nullspace baseline. If this addendum and the
parent contract can be read differently, the interpretation that produces the
larger comparison population or the stricter failure outcome applies.

Workflow permission is governed by the verbatim 2026-08-07 authorization in
`project/cp2_full_completion_authorization_20260807.txt` and its binding record.
It supersedes conflicting earlier restrictions on dataset/result access,
iterative debugging, recovery, and global exactly-one execution attempts.
References below to one campaign or one execution describe the internally
complete population and command lifecycle of each declared attempt; they do
not impose a lifetime one-attempt limit.  Development and rehearsal runs are
nonformal.  A failed declared evidence attempt must be retained and reported;
a corrected attempt requires a new source or profile identity and a new
declaration.  No attempt may be silently deleted, relabeled, concealed, or
made passing by a result-driven threshold change.

## Frozen recorded-data scope

Each declared CP2-C evidence attempt is one deterministic campaign over all
three CP2 sequences. It processes each sequence to completion and does not
stop after the thousandth counted record.

| sequence | frozen offset | frozen bag SHA-256 |
| --- | ---: | --- |
| `MH_01_easy` | 40 s | `57f440ccd68ec8dc8f9461269f5909656b86198bac3adfd677b1fcc7a1428fa9` |
| `MH_03_medium` | 5 s | `c51b0064681dfb287b6653f5fd54e6c56af5d9151c866e17574fdbc527db2311` |
| `V1_01_easy` | 0 s | `6dc6192fac63dd0a05ba745548b41fe8cae14724168a98865a81d37e681bbc81` |

The per-sequence and aggregate attempted/outcome counts are retained. At least
1,000 aggregate records must satisfy the baseline-only counted-record rule.

## Shared raw seam and shadow authority

For updater invocation `u`, let `R_u` be the ordered features for which
`UpdaterHelper::get_feature_jacobian_full` produced one raw
`(H_x,H_f,r,Hx_order)` system. Before the in-place Givens call, the Schur shadow
receives owning value copies of that exact system and a value-only covariance
layout. The shadow receives no live `State`, `Type`, `Feature`, or caller-owned
feature-vector pointer.

The shadow is an evidence capability orthogonal to
`up_msckf_landmark_elimination`; it is legal only while the live selected mode
is `nullspace`. It does not add an elimination mode. CP2-E runs use the same
timer instrumentation with the shadow capability disabled.

The nullspace path alone controls feature erasure, `Feature::to_delete`,
accepted-feature lifecycle, the live stacked system, and the sole
`StateHelper::EKFUpdate` call. The Schur path owns a distinct accepted set,
stack, compressor output, retained gamma, and read-only proposal. Both global
proposals are computed before the baseline commit from the same immutable
pre-mutation prior snapshot.

## Gate-agreement population

For each `f` in `R_u`, let `m_uf` be its pre-elimination raw row count. Define
`G_N,u` and `G_S,u` as the features for which the nullspace and Schur paths,
respectively, produced all of the following:

1. a mode-valid reduction;
2. a finite innovation system and successful factor/solve;
3. a finite NIS and finite threshold; and
4. an actual Boolean decision using the frozen strict `NIS > threshold`
   rejection rule.

For Schur, mode-valid means `SchurUpdate::Reduce` returned `kAccepted` under
the ordered CP1 policy. For nullspace, mode-valid means the unchanged
production Givens call ran with the configured representation, produced
exactly `m-3` rows, and its reduced `H_x`, residual, and whitened gamma were
finite. The baseline does **not** acquire the candidate SVD rank,
conditioning, or Householder acceptance policy. A diagnostics check may
observe a baseline failure, but it may not introduce a new live-baseline
rejection or change feature lifecycle.

Every updater invocation with a shared raw seam enters the official CP2-C
agreement totals, including `all_rejected`, `empty_after_compression`, and
`preflight_rejected` invocations. The unweighted denominator and numerator are

```text
D = sum_u |G_N,u union G_S,u|
M = sum_u sum_{f in G_N,u intersection G_S,u} [decision_N(u,f) = decision_S(u,f)].
```

A one-sided missing gate decision is denominator-only and therefore a
disagreement. A feature for which neither path reaches a decision is fully
traced but is not a gate-decision attempt. The raw-row-weighted totals are

```text
D_row = sum_u sum_{f in G_N,u union G_S,u} m_uf
M_row = sum_u sum_{f in G_N,u intersection G_S,u,
                  decision_N(u,f) = decision_S(u,f)} m_uf.
```

Raw rows `m_uf` are used, never `m_uf-3`, observation counts, accepted rows, or
compressed rows. The runner retains `|R|`, both per-path gate-attempt counts,
the union and intersection counts, and every numerator and denominator.
Duplicate feature IDs within an invocation make the evidence invalid.

The two 0.999 gates are evaluated without rounded floating-point comparisons:

```text
D > 0 and 1000*M >= 999*D
D_row > 0 and 1000*M_row >= 999*D_row.
```

Terminal accept/reject agreement over `R_u` may be reported as a secondary
diagnostic but cannot replace these official totals. Every disagreement has
one explicit class. Each path has exactly one gate-stage value, using this
precedence:

1. `reduction_unavailable`;
2. `innovation_nonfinite`;
3. `factorization_failed`;
4. `solve_or_nis_nonfinite`;
5. `threshold_nonfinite`; or
6. `decision`.

For both paths, one shared production gate helper evaluates the gate from the
mode's emitted unwhitened reduced `H,res`, the immutable pre-mutation prior
snapshot, and the raw system's ordered `(covariance ID,size)` layout. It
constructs `P_marg` by copying the corresponding prior covariance blocks in
that exact layout order, evaluates
`S=H*P_marg*H.transpose()`, then executes
`S.diagonal() += sigma_px_sq*Eigen::VectorXd::Ones(S.rows())`. It observes
shapes and finiteness of `H,res,P_marg,S` after those exact expressions and
before factorization, without rewriting an operand. It then constructs
`Eigen::LLT<Eigen::MatrixXd> gate_factor(S)` with the default lower triangle.
If factorization succeeds, it evaluates, in order,
`solved_residual=gate_factor.solve(res)` and
`chi2=res.dot(solved_residual)` before testing the post-solve factor status,
the solved vector, and chi2 for success/finiteness.

For reduced row count `q`, the helper obtains `chi2_check` from the updater's
constructor-built Boost 0.95 chi-square table when `q<500`, and otherwise from
a newly constructed `boost::math::chi_squared(q)` and
`boost::math::quantile(distribution,0.95)`. It forms `threshold` with exactly
one binary64 `chi2_multiplier*chi2_check` multiplication. The evidence stage
uses the precedence above: a nonfinite innovation is `innovation_nonfinite`;
otherwise a failed LLT is `factorization_failed`; otherwise a failed/nonfinite
solve or chi2 is `solve_or_nis_nonfinite`; otherwise a nonfinite quantile or
threshold is `threshold_nonfinite`; otherwise `decision` stores acceptance as
Boolean `!(chi2>threshold)`, so `true` means accept and equality is accepted.

The resolved multiplier and all constructor-table entries `q=1..499` are
required finite during CP2 startup. The helper still evaluates the unchanged
production comparison for lifecycle after any dynamically computed
`q>=500` threshold: if that threshold is nonfinite the evidence decision is
null and the campaign fails, while actual baseline/candidate accepted-list
membership follows the unmodified `chi2>threshold` expression. Thus an
observational finite check never adds a live-baseline rejection. Both modes
call the helper under strict no-fast-math and no-floating-point-contraction
flags; there is no repair, alternate solve, re-evaluation, or candidate-only
gate policy.

Every raw-seam feature has exactly one agreement class:
`both_match_accept`, `both_match_reject`,
`boolean_nullspace_accept_schur_reject`,
`boolean_nullspace_reject_schur_accept`, `nullspace_only`, `schur_only`, or
`neither_decision`. The per-path stage and exact reducer/preview status refine
the three unavailable classes. The first two classes sum to `M`; the two
Boolean mismatches plus both one-sided classes sum to `D-M`. Their raw-row
weights similarly reconcile exactly to `D_row-M_row`. `neither_decision` is
outside `D` and is retained separately. No other class is valid.

## Per-feature mathematical evidence

Whenever both reductions are valid, the nullspace reference statistics are

```text
Lambda_N = H_N^T H_N / sigma_px^2
eta_N    = H_N^T r_N / sigma_px^2
gamma_N  = r_N^T r_N / sigma_px^2.
```

For the retained binary64 evidence, evaluate that mathematical definition in
this exact order: form `A_N=H_N.array()/sigma_px` and
`b_N=r_N.array()/sigma_px` by direct elementwise division; require both finite;
form `raw_Lambda_N=A_N.transpose()*A_N`; then evaluate its induced infinity
norm symmetry error with the exact production expression
`raw_Lambda_N.size()==0 ? 0.0 :
(raw_Lambda_N-raw_Lambda_N.transpose()).cwiseAbs().rowwise().sum().maxCoeff()`.
Require the raw matrix and that error finite before setting
`Lambda_N=0.5*(raw_Lambda_N+raw_Lambda_N.transpose())`,
`eta_N=A_N.transpose()*b_N`, and `gamma_N=b_N.squaredNorm()`. Do not form or
multiply by `1/(sigma_px*sigma_px)`. The Schur values are the production
reducer's already-whitened, post-raw-symmetry-check values in
`SchurReductionResult`; they are not recomputed from the emitted unwhitened
rows. All evidence translation units use the same strict no-fast-math,
no-floating-point-contraction policy as the production reducer.

The Schur statistics are compared using Frobenius, Euclidean, and absolute
errors, respectively, with the already-frozen CP2 statistics tolerance
`1e-10 + 1e-8*||reference||`. For every raw system in every invocation where
both reductions are mode-valid, the trace retains each error, reference norm,
tolerance, normalized ratio and pass flag. Every available diagnostic must be
finite and every passing row's ratio must be at most one; an unavailable,
nonfinite, or out-of-tolerance comparison is a CP2-C campaign failure,
including on a noncounted update. Each
mode gates its own reduced system using the same prior marginal, scalar
variance, chi-square multiplier and 0.95 quantile, with `q=m-3`. Unavailable
values retain their actual unavailability stage; a finite substitute is
forbidden.

The reference norm and error are evaluated with Eigen's binary64 Frobenius
matrix norm, Euclidean vector norm, and `std::abs` scalar operation. Then form
the tolerance as the binary64 multiplication followed by addition shown above
and the ratio as one division. These values are evidence diagnostics; the
exact pass rule remains `error<=tolerance`, never a rounded printed ratio.

On every passing record, a path accepts a feature only after reduction,
factorization, solve, finite NIS/threshold, and a non-rejecting strict
comparison. Failed evidence also retains actual unchanged lifecycle membership
as defined above. For each path,

```text
Gamma_u = sum_{f in accepted_u, in processing order} gamma_uf
```

is accumulated with binary64 additions after feature acceptance and before
global compression. It is never reconstructed from the compressed residual.
A nonfinite candidate sum makes the candidate global proposal unavailable and
fails a baseline-counted record, but cannot suppress the baseline commit. At
its first nonfinite candidate sum, later candidate gamma additions are skipped
while Schur reduction, statistics, gating, and accepted-ID/digest collection
continue over every remaining shared raw system; only candidate global
compression/preview is suppressed. Baseline gamma is likewise evidence-only:
a nonfinite running sum records a null gamma with an explicit nonfinite status
and fails the campaign math, while baseline feature lifecycle, compression,
preflight, commit, counted status, and terminal status continue exactly as if
the diagnostic accumulator did not exist.

## Accepted-feature digests

Every path retains both its accepted set and accepted processing sequence.
Their digests are SHA-256 over these exact byte encodings:

- set domain: ASCII `SchurVIO-CP2-accepted-set-v1` followed by one zero byte;
- sequence domain: ASCII `SchurVIO-CP2-accepted-sequence-v1` followed by one
  zero byte;
- an unsigned 64-bit big-endian element count; then
- each feature ID as unsigned 64-bit big-endian, sorted strictly ascending for
  the set digest and retained in actual order for the sequence digest.

The set input must contain unique IDs. The trace retains the IDs as well as
both 64-character lowercase hexadecimal digests so an independent verifier can
recompute them.

## Proposal comparison and complete state partition

The baseline preview is the reference. For every semantic error-state block
`B_i`, retain

```text
e_x(i) = ||dx_S[B_i] - dx_N[B_i]||_2
t_x(i) = 1e-8 + 1e-6*||dx_N[B_i]||_2.
```

For every ordered block pair `(i,j)`, including both covariance triangles,
retain

```text
e_P(i,j) = ||P_plus_S[B_i,B_j] - P_plus_N[B_i,B_j]||_F
t_P(i,j) = 1e-8 + 1e-6*||P_plus_N[B_i,B_j]||_F.
```

Each record retains the error, reference norm, tolerance, normalized ratio,
comparison status, availability, and pass flag. For a state block, evaluate
the baseline-reference Eigen binary64 Euclidean norm, form each
candidate-minus-reference coefficient difference, evaluate the difference's
Eigen Euclidean norm, form the tolerance by the binary64 multiplication
`1e-6*reference_norm` followed by the addition `1e-8+product`, and form the
ratio with one binary64 `error/tolerance` division. For a covariance block use
the same order with Eigen Frobenius norms. Evidence translation units use
strict no-fast-math and no floating-point contraction. The exact pass rule is
`error<=tolerance`; every available ratio must be finite, and a passing row's
ratio is at most one. The
absolute tolerance is applied once per whole block, not once per coefficient.

The precommit active-state partition is ordered by covariance ID and covers
every coordinate exactly once:

- IMU `theta`, position, velocity, gyro bias, and accelerometer bias, each of
  size three;
- every clone, keyed by its exact binary64 timestamp, split into `theta` and
  position blocks of size three;
- every active in-state SLAM landmark, keyed by feature ID, representation and
  anchor metadata, as its full error-state block.

The frozen profile must assert that camera and IMU calibration variables are
not active. An unexpected active type or a gap/overlap in the partition fails
the evidence before hashing; it is never skipped. IDs and sizes
come from the live `Type` metadata rather than guessed offsets. Fixed
calibration values and camera-model caches are part of the zero-write snapshot
even though they are not covariance blocks.

If the baseline commits but the candidate has no accepted system, an empty
compressed system, a rejected preflight, nonfinite gamma, or any other missing
`dx,P_plus`, the baseline record remains counted and is an immediate CP2-C
failure. One unavailable, failed block row is still retained for every state
block and ordered covariance block pair. A missing candidate, nonfinite
reference norm, nonfinite coefficient difference or error norm, nonfinite
tolerance, or nonfinite ratio has an explicit first-failure status; all four
numeric diagnostics are null and its comparison-availability and pass flags
are false. Candidate availability is retained independently. No block row is
omitted or declared vacuous, no later arithmetic is attempted after the first
failure, and no synthetic finite or zero proposal is substituted.

## Counted records, writes, and terminal taxonomy

A counted record is determined only by the live baseline: at least one
accepted feature, a valid nonempty globally compressed system, an accepted
baseline preflight, and exactly one returned `StateHelper::EKFUpdate` call.
All updater invocations reconcile into exactly one of:

- `empty_input`;
- `all_rejected` (with no-features-after-cleaning,
  no-features-after-triangulation, no-raw-systems, or
  all-baseline-features-rejected subreason);
- `empty_after_compression`;
- `preflight_rejected`;
- `committed_counted`; or
- `internal_failure`.

Candidate outcome is separate and never controls that taxonomy.

Before shadow work in every invocation with at least one shared raw system,
snapshot the covariance, every active type's nominal and FEJ values, the
complete state-block inventory, state timestamp, fixed calibration values, and
camera-model cache values. Recapture the same fields after all shadow work and
immediately before the baseline commit, or immediately before the terminal
return when the baseline does not commit, and require bitwise identity. Thus
noncommitting invocations do not evade the zero-write proof. Candidate counters
are exactly zero for EKF-update calls, mean/`Type::update` writes, covariance
writes, and feature lifecycle writes. Both previews are read-only. A counted
baseline has exactly one EKF-update call, one transaction-level mean commit and
one covariance commit.

Before committing, clone every active top-level `Type` from the snapshot and
apply the corresponding baseline-preview `dx` segment exactly once to each
clone, in ascending covariance-ID order. The expected per-Type update-call
count is therefore the number of active top-level types and is retained. After
the one live `StateHelper::EKFUpdate` call returns, require bitwise equality of
the complete live covariance with the baseline preview's `P_plus`, bitwise
equality of every live nominal value with its updated clone, and bitwise
equality of every FEJ value with the precommit snapshot. Retain the compared
field counts and mismatch counts; all mismatch counts must be zero. This exact
check is stricter than the mixed CP2 block tolerances and cannot be replaced by
a rounded norm summary.

Detached clones exist only in the trusted live-commit oracle and are never
passed to the candidate. Landmark representation, feature, camera, and anchor
identity is retained separately from `Type::clone`; a `Vec`-typed landmark
clone may predict additive nominal update bytes but cannot define or erase that
dynamic metadata.

Every reducer and preview jitter, repair, alternate-solve, clamp,
regularization, silent-fallback, and fallback counter is retained per record
and totals exactly zero.

## Canonical SHA-256 serialization

All C++-produced mathematical digests use SHA-256, lowercase hexadecimal, and
domain-separated canonical bytes. Unsigned integers are big-endian. Signed
nonnegative IDs and dimensions are encoded after validation as unsigned
64-bit. A binary64 value is encoded as its exact IEEE-754 bit pattern in
big-endian order; `-0` is therefore distinct from `+0`. Strings are UTF-8,
preceded by an unsigned 64-bit byte length. A dense matrix is encoded as its
unsigned 64-bit row count, unsigned 64-bit column count, then row-major values.
A vector uses `(rows,1)` matrix encoding. A list begins with its unsigned
64-bit element count.

The following domains and payloads are frozen:

- `SchurVIO-CP2-raw-system-v1\0`: feature ID, raw `H_x`, raw `H_f`, raw
  residual, then the ordered `(covariance ID,size)` Jacobian layout;
- `SchurVIO-CP2-prior-snapshot-v1\0`: state timestamp, covariance, ordered
  block inventory, ordered active nominal/FEJ values, fixed calibration values,
  and ordered camera-cache values, using the exact composite layout below;
- the accepted-set and accepted-sequence domains specified above.

The singular `config_sha256` used by trace rows is the digest of the retained
static-configuration bundle payload. Its domain is
`SchurVIO-CP2-static-config-v1\0`, followed by a u64 count of four and these
records in literal order: `config/euroc_mav/estimator_config.yaml`,
`config/euroc_mav/kalibr_imu_chain.yaml`,
`config/euroc_mav/kalibr_imucam_chain.yaml`, and
`project/cp2_serial.launch`. Each record is its length-prefixed path, u64 byte
size, and 32 raw SHA-256 bytes. The bundle bytes are retained. This digest does
not replace the individual file identities or the resolved-parameter digest.

For CP2-C, every final artifact-record `pair_index_sha256` is the ordinary
SHA-256 of the complete retained `serial_pairs.jsonl` bytes for the
three-sequence campaign. For a CP2-D sequence artifact it is the ordinary
SHA-256 of that artifact's complete `pair_index.jsonl`. It is never a hash of
an in-memory row subset or a JSON re-serialization. The immutable pre-run
runtime context instead carries null for this field because the complete trace
does not exist yet; deterministic post-run assembly computes and joins the
nonnull digest without rewriting that context.

File, source, executable, DSO, bag, configuration, resolved-parameter, launch,
script, pair-index, and artifact-integrity hashes are ordinary SHA-256 over the
retained bytes. Resolved ROS parameters are required in addition to static
configuration-file hashes.

The prior-snapshot composite payload is encoded in this exact order:

1. state timestamp as binary64;
2. the full covariance matrix;
3. the semantic-block list, in strictly increasing covariance-ID order;
4. the active top-level-type list, in strictly increasing covariance-ID order;
5. the fixed-IMU-calibration list in the literal order `dw`, `da`, `tg`,
   `gyro_to_imu`, `accel_to_imu`;
6. the fixed camera-to-IMU time-offset value;
7. the per-camera fixed-calibration list, sorted by unsigned camera ID; and
8. the per-camera model-cache list, sorted by unsigned camera ID.

Each semantic-block entry is: length-prefixed kind string, covariance ID,
error size, then kind identity. Kinds and identities are exactly
`imu.theta`, `imu.position`, `imu.velocity`, `imu.gyro_bias`, and
`imu.accel_bias` with no additional identity; `clone.theta` or
`clone.position` followed by the clone timestamp binary64; or
`slam_landmark` followed by feature ID, length-prefixed representation string,
signed camera ID, and anchor timestamp binary64. Clone timestamps must be
finite and strictly numerically increasing; their binary64 bits are retained.
Landmarks are sorted by covariance ID, while their IDs must also be unique.
No fallback kind is permitted on the frozen profile: an otherwise unknown
active type fails evidence before hashing.

Each active top-level-type entry is: length-prefixed type tag, covariance ID,
error size, kind identity, nominal matrix, then FEJ matrix. Type tags are
exactly `imu`, `clone`, or `slam_landmark`. The `imu` kind identity has zero
fields and contributes zero bytes after error size; it is not a
length-prefixed empty string. A clone identity is its timestamp binary64. A landmark identity is feature ID, length-prefixed
representation string, signed camera ID, and anchor timestamp binary64. The
IMU entry precedes clones and landmarks naturally by covariance ID.

Each fixed-IMU-calibration entry is its literal tag, nominal matrix, and FEJ
matrix. The time-offset field is its nominal matrix followed by its FEJ matrix.
Each per-camera fixed-calibration entry is camera ID, extrinsic nominal,
extrinsic FEJ, intrinsic nominal, and intrinsic FEJ. Each camera-cache entry is
camera ID, the literal model tag `CamRadtan`, signed width, signed height, and
the cache calibration matrix returned by `CamBase::get_value`, the 3-by-3
binary64 matrix returned by `CamBase::get_K`, and the 4-by-1 binary64 vector
returned by `CamBase::get_D`, all converted without arithmetic and encoded
row-major. These three fields are the complete mutable `CamBase` calibration
cache. Signed integers
are encoded as big-endian 64-bit two's-complement. All maps are rejected if
their key sets differ from the configured camera-ID set. Every matrix and
metadata field is retained alongside the digest so the independent verifier
can reconstruct the payload and compare the pre-shadow and precommit snapshots
field by field.

Resolved ROS parameters use the domain `SchurVIO-CP2-ros-params-v1\0` and a
recursive typed encoding. A map is tag `m`, element count, then entries sorted
by UTF-8 key bytes, each entry containing a length-prefixed key and encoded
value. A list is tag `l`, element count, then values in order. Scalars are tag
`b` plus byte `00`/`01`, tag `i` plus signed 64-bit two's-complement, tag `f`
plus binary64, or tag `s` plus a length-prefixed UTF-8 string. Null, nonfinite
floats, integers outside signed 64-bit, duplicate keys, and other YAML/XML-RPC
types are invalid. The raw ROS parameter dump and the canonical payload bytes
are both retained.

## Trace identity and timing separation

The exact `cp2_trace_level` values are `recorded_full`, `sequence`, and
`timing`. `recorded_full` requires live `nullspace` plus an enabled Schur shadow
and is the only CP2-C setting. `sequence` requires the shadow disabled and is
used by each independently selected CP2-D live mode. `timing` requires the
shadow disabled and is reserved for CP2-E. Any other spelling or combination
fails before feature processing; an ordinary non-evidence launch does not use
`project/cp2_serial.launch`.

Sequence indices are fixed as `0=MH_01_easy`, `1=MH_03_medium`, and
`2=V1_01_easy`. A filtered-message ordinal is the zero-based position in the
serial runner's post-offset rosbag view after retaining only the configured IMU,
cam0, and cam1 topics, in rosbag iteration order. A pair index is the zero-based
contiguous ordinal of a stereo pair selected by the frozen serial
forward-search/deduplication rule within that sequence: scan filtered messages
in order; skip a previously used camera message; for a camera anchor, inspect
the first later filtered message from the other camera and accept only when the
absolute difference of their exact rosbag record-time nanoseconds is strictly
less than 20,000,000; on acceptance mark both camera ordinals used. Do not
search past that first later other-camera message and do not select a nearest
message. A selected pair belongs to exactly one sequence and contains exactly
one cam0 and one cam1 filtered-message ordinal. Camera timestamp
nanoseconds are the exact nonnegative ROS cam0 image-header stamp encoded as
`1,000,000,000*sec+nsec`, with `0<=nsec<1,000,000,000` and checked integer
overflow. They are never reconstructed by rounding `State::_timestamp`.

Within each sequence run, updater invocations have zero-based contiguous
invocation IDs in call-entry order. Feature ordinals are zero-based in the
shared raw-system processing order. Trace rows
use the tuple
`(sequence_index,pair_index,invocation_id,feature_ordinal)` as applicable and
also retain camera timestamp nanoseconds and feature ID.

The first executable statement in `UpdaterMSCKF::update` captures
`std::chrono::steady_clock::now()`. Every early return captures its endpoint as
the last timed estimator operation on that path. The committing endpoint is
captured as the first operation after `StateHelper::EKFUpdate` returns. No
estimator computation occurs after an endpoint: excluded sink serialization
follows, then the function returns. Durations are nonnegative integer
nanoseconds. Shadow-enabled durations are labeled functional CP2-C diagnostics
and are ineligible for CP2-E.

For a declared formal artifact to be eligible, all five preregistered CP2 entry
points must exist, their artifact-free self-tests must pass under their
bag-access prohibition, the complete CP1/CP2 unit suite must pass at the exact
clean runtime commit, and a fresh unit artifact must anchor that commit before
the campaign opens its bag.  The current authorization separately permits bag,
registry, ground-truth, and recorded-result access during implementation,
diagnosis, and rehearsal; such access and outputs are development-only and
must not be represented as formal evidence.
