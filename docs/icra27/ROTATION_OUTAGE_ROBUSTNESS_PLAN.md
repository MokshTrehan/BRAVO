# S1 KAIST `rotation` outage-recovery plan

Status: **ACTIVE DEVELOPMENT**  
Frozen S1 base: `49a653ab3c6e69f436aadb749d1f8238624288e6`  
Development branch: `schurvio-lite/rotation-outage-robustness`  
Development sequence: KAIST `rotation`  
Regression suite: the other ten KAIST sequences  

## Scope and evidence boundary

This work improves S1 only. The pinned upstream OpenVINS U0 checkout, the
frozen S1 checkout, and all G0.5/v3 evidence are immutable. `rotation` is
development data, so a result on it is not independent evidence of general
robustness. A candidate may advance only after the other ten KAIST sequences
pass the prospective regression gates below.

The change is an outage-recovery mechanism, not a Schur change. Do not change
the initializer, calibration, exact-header pairing, landmark-elimination mode,
visual-pass count, clone count, bag bytes, or timestamps. Do not use ground
truth at estimator runtime. Do not relax MSCKF or SLAM gates globally.

Late initialisation is reported but is not a failure for this development
study, as requested by the user. Every failed, partial, no-init, crash, and
rejected-recovery attempt remains an append-only result.

## Frozen control and diagnosis

The isolated clean rebuild reproduced the frozen results exactly:

| Sequence | State SHA-256 | Deviation SHA-256 | TUM SHA-256 |
|---|---|---|---|
| `rotation` | `9782c8d9b740c3b953b0c180be0975809759c7e3738b0c53e32ae4aab2422cb8` | `2c1081efe68e040470fb44693b5c1647f04c8c7440702d534f21f156c14a37fe` | `024dc5d78b2ac26b628d32a1eaccc7c9fd20b210e2e7adc523f7409942672714` |
| `rotation_fast` | `4da89a22a63259311ecfc0b3f8706628d91798919bea25d6175710ec8e5f873b` | `f8a3e74f2706f37af4908c3a699bf29254fdeedc2f4c8aaaed0fe50ddea4e2cb` | `6fb4f7b4bbafa3e3d40479ce49e5a406c8b122199012a1ee0c50f7e64f1ff34c` |

The target contains a 13.441997175 s selected-stereo outage, while all other
observed post-initialisation output gaps are at most 0.106 s. IMU delivery is
continuous across the outage. S1 preserves attitude but propagates about
7.92 m while ground truth moves about 0.046 m. Its first resumed position is
about 7.87 m wrong, its full aligned ATE is 4.257743 m, and almost all normal
visual recovery updates are rejected.

The retained map is sufficient for a nonlinear recovery attempt: an offline
left-camera PnP/RANSAC check found 19 inliers from 22 valid retained
landmark/observation correspondences at a 2 px threshold and recovered the IMU
position to roughly 5 cm of the last trusted position. This motivates a
fail-closed relocalisation boundary; it does not justify relaxing the EKF.

## Frozen recovery contract

The production default is disabled. When enabled, recovery is generic and may
not inspect a sequence name or ground truth.

1. Trigger once when an initialized estimator receives a stereo callback more
   than **0.5 s** after its live state timestamp.
2. Keep the live state unchanged while evaluating recovery. Ordinary
   propagation, MSCKF updates, and SLAM updates are forbidden in this phase.
3. Use only current image measurements whose feature IDs refer to retained
   live SLAM landmarks. Convert every retained landmark to the global frame,
   then solve a left-camera PnP/RANSAC problem using the frozen calibration.
4. Accept one pose only if all checks pass:
   - at least 12 valid 3D--2D correspondences;
   - at least 12 inliers at a 2 px reprojection threshold;
   - inlier ratio at least 0.70;
   - finite pose and residuals, positive depth for every inlier;
   - non-collinear 3D support and inlier image bounds spanning at least 25% of
     both image width and height;
   - PnP-versus-IMU orientation disagreement no greater than 5 degrees.
5. Require **three consecutive accepted poses**. Their global positions must
   remain within 0.10 m of their component-wise median. This is the prospective
   translational-stationarity gate that licenses resetting velocity to zero.
6. On success, perform a reanchor reset rather than an EKF update: build a
   fresh state at the last verified global pose, preserve fixed calibration
   and the last trusted IMU biases, set velocity to zero, discard every old
   clone/landmark/feature measurement, seed KLT from the commit image, and warm
   up a new visual epoch. Use conservative covariance floors of 2 degrees for
   attitude, 0.10 m for position, and 0.05 m/s for velocity.
7. If any gate fails, do not mutate the state. After ten attempted recovery
   frames, mark recovery failed and remain degraded. A later segmented restart
   may be studied separately, but it must never be silently stitched into the
   old global trajectory.

Every trigger, attempt, rejection reason, accepted pose, reset, and warm-up
transition must be machine-readable. The raw pre-gap and post-reset sparse
geometry streams are separate epochs; a combined atlas is allowed only after
a verified PnP reanchor.

## Candidate order and kill rules

The order is fixed and stops at the first full pass:

1. `C0`: frozen KLT control (`rotation` and `rotation_fast`) -- complete and
   byte-identical.
2. `C1`: the single diagnostic global descriptor change
   `use_klt: true -> false`, with every other parameter fixed. It is diagnostic
   because native descriptor tracking is not the offline gap bridge.
3. `C2`: default-off, gap-gated PnP reanchor while normal operation remains
   frozen KLT.
4. `C3`: explicitly segmented restart only if `C2` rejects safely or fails.

No threshold sweep or combinatorial tuning is allowed. Kill a candidate on
its first target run if it crashes, produces nonfinite output, accesses ground
truth, changes the pairing census, fails to reach the end, or improves the
cross-gap translation error by less than 50%. Retain the failed attempt.

## Target acceptance

The selected candidate must pass three deterministic `rotation` runs:

- exactly one recovery activation and one verified reanchor per run;
- finite, strictly increasing state/deviation output and terminal gap at most
  0.10 s;
- unchanged exact-header input accounting and no pending/decode failures;
- cross-gap relative translation error at most 0.50 m;
- cross-gap relative rotation error at most 1.534 degrees;
- full SE(3)-aligned translation ATE at most 0.25 m;
- independently aligned pre-gap ATE at most 0.065795 m;
- independently aligned post-gap ATE at most 0.25 m;
- 1 m translation RPE at most 0.25 m and rotation RPE at most 3.0601 degrees;
- first-resumed position NEES at most 11.345 when the full 3x3 covariance is
  available; otherwise consistency is `UNASSESSABLE`, not a pass;
- identical state, deviation, and TUM hashes across the three serial repeats;
- a complete raw geometry capture and deterministic atlas with no detached
  post-gap trajectory branch.

## Regression acceptance

Run `rotation_fast` first as the sentinel, then the remaining nine sequences.
For the gap-gated implementation, the recovery activation count must be zero
on all ten. Their state, deviation, and TUM files must be byte-identical to the
frozen S1 outputs; timing values may differ, but row timestamps and counts may
not. Every run and its qualitative replay is retained, including no-init or
failure prefixes.

The global descriptor diagnostic, if run, has a weaker but still mechanical
gate: every available ATE/translation-RPE/rotation-RPE ratio must be at most
1.20 and each metric's median ratio over the ten must be at most 1.10. It is
not eligible as the final fix without this full regression pass.

## Artifacts and status ledger

All development artifacts live outside the frozen evidence roots under:

`/home/moksh/schurvio-icra27-artifacts/rotation-robustness/`

Each attempt stores source commit/tree/diff, config/launch/build/binary/library
identities, bag hashes, resolved parameters, command/environment, console and
resource logs, state/deviation/timing/TUM, quantitative metrics, gap and
recovery diagnostics, status/error, raw geometry bag (or explicit absence),
derived PLY/SVG atlas, manifest, and `SHA256SUMS`. Nothing is overwritten or
substituted.

## Decision states

- `ACTIVE`: bounded implementation or a scheduled gate is running.
- `CANDIDATE_REJECTED`: a candidate failed a kill or acceptance rule; evidence
  is retained.
- `VALIDATED_ON_KAIST11`: target 3/3 and all ten regressions pass. This is a
  KAIST result, not a general robustness claim.
- `STOPPED_NEGATIVE`: bounded candidates are exhausted without a pass.

