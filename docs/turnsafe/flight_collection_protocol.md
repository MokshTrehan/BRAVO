# TurnSafe-MSCKF v6 Flight Collection Protocol

**Status:** Session 0 protocol freeze

**Scope:** custom recorded-bag acquisition and quarantine; human-operated data
collection, not an estimator implementation task

**Controlling source:**
[`IMPLEMENTATION_PLAN_V6.md`](guidance/IMPLEMENTATION_PLAN_V6.md), especially
Sections 1, 7.6--7.8, 13.3--13.4, 15 (parallel human lane), 16, and 18.

This protocol freezes an outcome-blind collection process for paired offline
evaluation. It does not authorize aircraft operation, later-stage code, or
holdout access.

## 1. Acquisition design

Collect exactly 90 valid events in the full factorial design:

```text
3 maneuver classes x 3 severity bands x 2 exposure conditions x 5 repeats
```

The maneuver classes are:

1. near-hover snap yaw;
2. coordinated arc turn;
3. forward-motion yaw/turn.

Each class uses three predeclared, monotonically increasing severity command
profiles and two predeclared exposure conditions. Five valid repeats are
required in every one of the 18 cells. The acquisition lead freezes the
numeric command envelope, allowed execution tolerance, exposure/gain settings,
lead-in duration, maneuver window, recovery duration, and abort boundary in the
custodian's acquisition record before the first scored event. These values are
not inferred from estimator performance or altered by a poor outcome.

The private acquisition record holds the exact randomized schedule and opaque
event identities. This tracked protocol deliberately creates no event list,
split seed, partition membership, or private manifest.

## 2. Preflight freeze and shakedown

Before collection, the human acquisition lead and data custodian record:

- aircraft, compute, camera, IMU, and reference/ground-truth hardware identity;
- global-versus-rolling-shutter status, image resolution/rate, IMU rate, and
  required recorded topics/streams;
- camera intrinsics, stereo extrinsics, camera--IMU extrinsics, time offset,
  estimator-independent reference calibration, and hashes of all calibration
  files;
- timestamp source, clock synchronization method and tolerance;
- whether exposure/gain is locked or recorded, plus the two exposure-condition
  settings and tolerances;
- maneuver command profiles, execution envelopes, physical operating volume,
  safety/abort criteria, and operator roles;
- bag format, recorder command/version, storage target, naming policy, and the
  hashing tool/version;
- outcome-blind sensor-quality bounds used by Section 5.

The August 12 shakedown verifies sensor streams, synchronization, calibration,
reference coverage, recorder capacity, and safe maneuver execution. Shakedown
bags are labeled non-study data and cannot replace one of the 90 valid events.
Collection cannot begin while any required numeric acquisition or validity
bound is unspecified.

## 3. Split, randomization, and information separation

Before flight, the data custodian performs a seeded, stratified split within
each maneuver/severity/exposure cell:

- two of five repeats per cell are development: 36 total;
- three of five repeats per cell are holdout: 54 total.

The custodian also randomizes acquisition order across cells so weather,
battery, temperature, and operator learning are not confounded with the fixed
cell order. The private seed, full acquisition order, partition membership,
holdout identifiers, and full manifest remain outside the agent-visible
worktree. Only a precommitted public split hash and, after validation, the
development manifest may be exposed during Sessions 0--6 (plan Sections 7.6
and 13.3).

The split is fixed before outcomes exist. A sensor-invalid replacement inherits
the original cell and privately assigned partition; it does not trigger a new
split or outcome-based reassignment. The invalid original remains in the
custodian ledger with its reason and hashes.

## 4. Per-event collection procedure

For each scheduled opaque event, the human operator follows this order:

1. Confirm the scheduled cell without revealing its partition.
2. Record the frozen vehicle, sensor, calibration, recorder, firmware, exposure,
   weather/site, battery, and operator metadata.
3. Start all required image, IMU, command, clock/synchronization, and
   ground-truth/reference streams; verify recording health without running or
   consulting a candidate estimator outcome.
4. Record the frozen lead-in, execute the scheduled maneuver once, and record
   through the frozen recovery interval. Apply safety abort criteria whenever
   needed.
5. Stop recording cleanly, make the raw bag read-only in the collection store,
   compute its hash using the frozen tool, and copy neither data nor identifiers
   into algorithm-development locations.
6. Run the immediate sensor-quality audit in Section 5, record pass/fail and
   machine-readable reason codes, then update the custodian ledger.

An aborted or sensor-invalid event is never edited in place. Preserve it and
schedule an outcome-blind replacement for the same cell. Algorithm trajectory,
error, reset, feature count, or perceived method quality is never a validity or
reshoot criterion.

## 5. Preregistered sensor-quality rules

Validity depends only on acquisition integrity, sensor/reference quality, and
execution of the predeclared maneuver envelope. The audit evaluates all of the
following against numeric bounds frozen before collection:

1. **File integrity:** recorder closed normally; the bag can be indexed/read;
   its size and cryptographic hash are recorded; no corruption or truncation is
   detected.
2. **Required streams:** every frozen required topic/stream exists, decodes, and
   covers the complete lead-in, maneuver, and recovery windows.
3. **Timestamp integrity:** timestamps are finite and monotonic under the frozen
   per-stream rule; duplicates, gaps, rates, and camera--IMU/reference skew stay
   within the frozen tolerances.
4. **Image integrity:** expected cameras, encoding, resolution, frame rate, and
   stereo ordering are present; corrupt/blank frames and dropout stay within
   the frozen bounds.
5. **IMU integrity:** expected axes, units, rate, finite samples, saturation,
   clipping, and dropout meet the frozen bounds.
6. **Exposure condition:** commanded exposure/gain or recorded metadata matches
   the assigned condition and tolerance. The actual metadata is preserved.
7. **Calibration/config identity:** sensor identity and all calibration/config
   hashes match the preflight freeze. Any intentional hardware change starts a
   separately declared acquisition block; it is not silently pooled.
8. **Reference integrity:** the preregistered ground-truth or defensible
   reference stream covers the evaluation interval and meets its frozen quality,
   synchronization, and coordinate-frame checks.
9. **Maneuver compliance:** estimator-independent command, IMU, or reference
   signals place the event within its scheduled class/severity execution
   envelope. Safety aborts fail this check and are retained as aborted attempts.

Each check emits `PASS` or one or more frozen reason codes. Numeric bounds and
boundary semantics are part of the signed acquisition preregistration; there
are no implicit defaults. A failed check is reshoot-eligible through August 15.
A passing event cannot be reshot because baseline or TurnSafe performs poorly.
Every attempt, including invalid and aborted attempts, remains counted in the
collection ledger (plan Section 13.3).

## 6. Calendar and completion gate

The early-flight calendar is frozen from plan Sections 15--16:

| Date (2026) | Required action |
|---|---|
| August 12 | Hardware/sensor/reference shakedown; freeze split and randomized acquisition order. |
| August 13--14 | Collect all 90 valid events; validate every bag immediately. |
| August 15 | Weather/hardware buffer and reshoots for preregistered sensor-invalid events only. |
| August 16 | Finalize bag hashes; expose 36 development events only; quarantine all 54 holdout events. |
| August 17 | Audit quarantine for the Session 2 branch decision; do not access holdout content. |

Collection is complete only when every cell has five sensor-valid events, all
attempts are accounted for, raw bags are immutable, hashes and calibration
identities are complete, 36 development events are exposed, and 54 holdout
events are quarantined. Schedule pressure never converts a sensor-invalid event
to valid.

## 7. Quarantine and chain of custody

Immediately after validation, the custodian stores holdout bags and the private
manifest in a separate read-only/quarantined location that is not mounted in or
visible to the agent workspace. Before the explicit Session 7 unlock, no agent
or algorithm-development process may mount, list, search, inspect, copy, hash
anew, summarize, plot, or evaluate holdout content, identities, private
membership, or private acquisition records. Accidental exposure is recorded as
a correctness incident and scientific decision-making stops (plan Sections
7.8 and 13.3).

The development release contains only:

- the 36 validated development bags;
- the development manifest with allowed event metadata and immutable hashes;
- calibration/reference material permitted for development; and
- the already committed public split-commitment hash.

The custodian retains the complete attempt ledger, raw-data hashes, private
seed/split/order, private manifest hash, replacement lineage, and access log
outside the agent-visible worktree. Public commitment verification must not
reveal private membership.

Only `APPROVE_SESSION_7_HOLDOUT_UNLOCK`, after source/config/threshold/evaluator
freeze, permits the one-shot paired offline holdout evaluation described in
[`evaluation_protocol.md`](evaluation_protocol.md). Unlock does not authorize
algorithm changes. Any post-unlock access, copy, or rerun is logged by UTC time,
operator, immutable input hash, command, output hash, and reason.

## 8. Required handoff records

The acquisition lead gives the custodian, without exposing private membership
to Sessions 0--6:

- signed acquisition preregistration and revision history;
- hardware/sensor/clock/calibration/reference identities and hashes;
- frozen cell definitions, numeric maneuver/exposure profiles, validity bounds,
  and reason-code dictionary;
- complete randomized attempt ledger and replacement lineage;
- immutable raw-bag and reference hashes;
- per-attempt sensor-quality audit and operator notes;
- the development manifest and public split commitment; and
- quarantine location/access-control attestation and access log.

All deviations are timestamped and explained before estimator replay. A
protocol deviation is reported; it is not silently repaired or hidden.

## 9. Optional onboard demonstration

After required paired offline evidence is complete and the method is frozen, a
safe onboard closed-loop demonstration may be recorded as video-only context.
It uses the frozen configuration, cannot change the evaluation protocol or
scientific claims, and is never a reason to risk aircraft, data integrity, or
the submission schedule (plan Sections 13.4, 15/Session 7, and 18).
