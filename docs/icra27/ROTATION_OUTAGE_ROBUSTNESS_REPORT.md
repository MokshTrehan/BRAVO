# S1 KAIST `rotation` outage-recovery report

- Status: **VALIDATED_ON_KAIST11**
- Evidence date: 2026-08-16
- Selected candidate: `C2-pnp-reanchor`
- Validated science commit: `2751bcdc0fae25c993b3224dc5fa40aaab6571d7`
- Science tree: `b3f9191b8bce3852ebff71e1ebf180181bcd6d05`
- Artifact root: `/home/moksh/schurvio-icra27-artifacts/rotation-robustness/r2-20260816T050224Z`
- Machine index: [`R2_ARTIFACT_INDEX.csv`](../../project/evidence/rotation_robustness/R2_ARTIFACT_INDEX.csv)

## Decision

C2 passes the prospective bounded study:

- KAIST `rotation` passes all target thresholds in three provenance-valid,
  byte-identical serial runs.
- The other ten KAIST sequences complete with zero recovery activations and
  state, deviation, and TUM outputs byte-identical to frozen S1.
- Every final sequence has a separate exact-trajectory qualitative replay with
  its raw sparse-geometry bag, PLY products, SVG atlas, manifest, and checksums
  retained.
- Pinned upstream OpenVINS U0 was not edited, rebuilt, or repaired during this
  study. The change is an S1 outage-recovery mechanism, not a Schur change.

This is a development-sequence plus locked-regression result on KAIST. It is
not independent evidence of generic robustness, it does not establish that
Schur elimination caused the result, and it does not revise the frozen G0.5
U0-versus-S1 conclusion.

## Frozen provenance

| Item | Frozen identity |
|---|---|
| Source commit | `2751bcdc0fae25c993b3224dc5fa40aaab6571d7` |
| Source tree | `b3f9191b8bce3852ebff71e1ebf180181bcd6d05` |
| Estimator executable SHA-256 | `0e46fa3e6ced2ff3eee568f6401ede3399e1a6f93e392c80db7a634cb50c700e` |
| Candidate config SHA-256 | `fa387a5c2146ef2a0471a4cf7acec392e43632a4e51983bb29f9ca8244d9d4b2` |
| Launch SHA-256 | `57a4bd1fa7fefdc84a73efe7c807a259b391858fbb1157fd8716afdd5770cc18` |
| CP0 build provenance SHA-256 | `4f221f2c62fbf07caae8ad959eedfa44f889e7c0cb6b05054291b60d3bc465a9` |
| Configure provenance SHA-256 | `f4b34d99a7ed281c4a54e134c22b0fbb898864f52fd4be05ec03c2b5e18d5396` |

The reporting commit follows the science commit. Every evidentiary run manifest
binds the clean science commit, compiled-input snapshot, executable, config,
launch, libraries, bag, command, environment, and post-close evaluator.

## Controls and bounded candidate choice

The final recovery-disabled controls are `C0-rotation-postcode-final` and
`C0-rotation_fast-postcode-final`. Both are `COMPLETED` at the validated science
commit and reproduce the frozen state, deviation, and TUM hashes exactly. C0
therefore proves the default-off implementation does not alter ordinary S1.

The single global-descriptor diagnostic C1 was rejected. Its first two starts
failed infrastructure gates; the first estimator-started attempt,
`r1-20260816T042739Z/C1-rotation-attempt-01c`, terminated with exit code `-6`
in the original descriptor path at OpenCV `batchDistance`. That negative result
is retained. The descriptor implementation was not repaired because it was not
the selected recovery mechanism.

C2 uses the frozen generic long-gap trigger, retained-landmark PnP gates,
three-pose consensus, and a declared fresh visual epoch. It neither relaxes
normal MSCKF/SLAM gates nor reads ground truth at runtime.

## Target result: KAIST `rotation`

All three valid runs report exactly one trigger, three state-unchanged recovery
attempts, one commit, zero failures, epoch 1, a complete commit covariance, and
five-clone warm-up. Input accounting is unchanged: 3,920 queued and processed
pairs, 767 frequency-thinned pairs, zero pending pairs, and zero decode
failures.

| Endpoint | Frozen C0 | C2 observed | Acceptance | Result |
|---|---:|---:|---:|---|
| Cross-gap translation error | 7.878093 m | 0.035133 m | <= 0.500 m | PASS |
| Cross-gap rotation error | 1.306451 deg | 0.397508 deg | <= 1.534 deg | PASS |
| Full translation ATE RMSE | 4.257743 m | 0.087813 m | <= 0.250 m | PASS |
| Pre-gap translation ATE RMSE | 0.054829 m | 0.054829 m | <= 0.065795 m | PASS |
| Post-gap translation ATE RMSE | 6.622667 m | 0.008709 m | <= 0.250 m | PASS |
| Translation RPE RMSE at 1 m | 1.381383 m | 0.084427 m | <= 0.250 m | PASS |
| Rotation RPE RMSE at 1 m | 2.550020 deg | 2.548349 deg | <= 3.0601 deg | PASS |
| First-resumed position NEES | unassessable | 8.696218 | <= 11.345 | PASS |

The full ATE falls by 97.94% and the cross-gap translation error by 99.55%
relative to frozen C0. These reductions describe this tuned KAIST target only.

### Deterministic repeats

| Included run | Status | State SHA-256 | Deviation SHA-256 | TUM SHA-256 |
|---|---|---|---|---|
| `C2-rotation-final-r2` | `COMPLETED` | `fbd7202f4e06927224f4b332f53672cbc47c1baa227af0124bd47354dd419cd3` | `52fb057315aa6150165244f07623c2dcfc6e34afe21875e77045c5b8356cebb8` | `65da602d676a792519e64726e3af809ab6d16dbd8a0df0b2696f29237178a571` |
| `C2-rotation-final-r3` | `COMPLETED` | same | same | same |
| `C2-rotation-final-r4` | `COMPLETED` | same | same | same |

`C2-rotation-final-r1` produced the same estimator outputs and passed the
numeric gates, but source bytes changed during its run when a harness-only
classification patch began. It is correctly retained as `INVALID_PROVENANCE`
and excluded from the 3/3 result.

## Locked ten-sequence regression

| Sequence | Scored run | Status | Recovery | Frozen output identity | Capture | Final active SLAM points |
|---|---|---|---|---|---|---:|
| `rotation_fast` | `C2-rotation-fast-regression-r1` | `COMPLETED` | 0/0/0, epoch 0 | exact | `C2-rotation_fast-capture-r1` | 49 |
| `circle` | `C2-circle-regression-r1` | `COMPLETED` | 0/0/0, epoch 0 | exact | `C2-circle-capture-r1` | 47 |
| `circle_fast` | `C2-circle_fast-regression-r1` | `COMPLETED` | 0/0/0, epoch 0 | exact | `C2-circle_fast-capture-r1` | 50 |
| `circle_head` | `C2-circle_head-regression-r1` | `COMPLETED` | 0/0/0, epoch 0 | exact | `C2-circle_head-capture-r1` | 49 |
| `infinite` | `C2-infinite-regression-r1` | `COMPLETED` | 0/0/0, epoch 0 | exact | `C2-infinite-capture-r1` | 49 |
| `infinite_fast` | `C2-infinite_fast-regression-r1` | `COMPLETED` | 0/0/0, epoch 0 | exact | `C2-infinite_fast-capture-r1` | 38 |
| `infinite_head` | `C2-infinite_head-regression-r1` | `COMPLETED` | 0/0/0, epoch 0 | exact | `C2-infinite_head-capture-r1` | 50 |
| `square` | `C2-square-regression-r1` | `COMPLETED` | 0/0/0, epoch 0 | exact | `C2-square-capture-r1` | 46 |
| `square_fast` | `C2-square_fast-regression-r1` | `COMPLETED` | 0/0/0, epoch 0 | exact | `C2-square_fast-capture-r1` | 49 |
| `square_head` | `C2-square_head-regression-r1` | `COMPLETED` | 0/0/0, epoch 0 | exact | `C2-square_head-capture-r1` | 14 |

All thirty regression identities—state, deviation, and TUM for ten
sequences—match the frozen ledger byte-for-byte. Exact values and manifest
bindings are in the machine index.

## Qualitative geometry archive

All eleven final cells have a separate capture replay whose state, deviation,
and TUM bytes equal its paired scored run. The archive retains:

- 11 closed raw feature-stream bags totaling 199,745,209 bytes;
- 77 PLY files and 33 fixed-view SVG files;
- 464 points across the eleven literal final active-SLAM states;
- 132/132 nested bundle checksums and 617/617 top-level artifact checksums
  independently recomputed successfully.

The geometry is deliberately labeled. `points_slam` is the current active
sparse filter state, not a cumulative map. `points_msckf` contains transient
last-update points, and `loop_feats` contains transient active tracks. The raw
bags are authoritative; none of these products is a dense or persistent
reconstruction.

Human review of the recovered `rotation` top, side, and oblique views finds no
detached post-gap trajectory branch. The top-view gap endpoints are close and
the numeric cross-gap error is 0.035 m. This is a qualified qualitative PASS,
not a substitute for the numeric result: the fixed GT viewport clips 264
estimate poses in the side view and 229 in the oblique view, and all 23 final
active-SLAM points lie outside those fixed viewports even though the raw PLY is
nonempty.

The `square` recorder began after nine pose publications. Its 4,036 point-topic
messages form a proved contiguous suffix of 4,045 pose outputs; nothing was
invented or backfilled. Thus its capture is valid under the frozen suffix
policy, but it is not evidence of one point-cloud snapshot for every pose.

## Retained exclusions and limitations

The artifact roots retain every failed or invalid attempt, including C1's
descriptor crash, early build/environment failures, the original C2 pilot,
the `%YAML:1.0` harness preflight failure, and the provenance-invalid C2 run.
No failed cell was overwritten or substituted.

Before treating C2 as generic production robustness, separately harden:

- exception atomicity across state replacement and frontend reconstruction;
- symmetric-positive-definite validation of retained bias covariance;
- the C++ runtime contract for IMU calibration/threading assumptions;
- raw-IMU endpoint bracketing in the orientation predictor;
- repeated outages during warm-up and bounded terminal buffers.

Those are follow-up engineering items. They do not alter the frozen
`VALIDATED_ON_KAIST11` decision, and this study stops here rather than tuning
past the first candidate that passed its prospective gates.
