# TurnSafe T1 pilot P1 contract

Status: frozen before the first real-data certificate scan

Authorization: `APPROVE_T1_PILOT_P1_CERTIFICATE_FACTOR_SHADOW_SCAN`

Repository identity: branch `schurvio-lite/turnsafe-t1-pilot`, commit
`4d2f2d275437ead9496b848730d5a9eb0e402864`, tree
`7c390aeb126f1ef69aad75733f40559639889ed3`.

## Scope and hard stop

P1 builds and tests a pointer-free certificate, rotation-bearing factor,
pair selector, and offline shadow evaluator. The core and scanner are not
linked into, called by, or reachable from `UpdaterMSCKF`, `VioManager`,
`StateHelper::CommitPrecomputedUpdate`, or any live proposal path. No P1
measurement row, sufficient statistic, correction, gate, or decision may
enter a live estimator proposal. P1 stops before P2.

No new estimator capture hook is authorized. The frozen Session-1E archives
already contain the exact development primitives required by P1. The offline
scanner may read only those archives and the public development metadata
named in the P1 preregistration.

## Entry and firewall

P1 requires all of the following:

- exact branch, commit, tree, and clean tracked tree/index;
- `P0B_CONTINUE_T1_PILOT_STRONG` and one of the two authorized P0C outcomes;
- complete P0B and P0C checksum-ledger validation;
- unchanged Session-2C `NO_EXTENSION`;
- readable public KAIST development corpus and Session-1E archives; and
- no holdout/private path, identifier, metadata, or content access.

The external-baseline evidence is context only. It cannot select, tune, or
amend any P1 certificate, consensus, spatial, rank, conditioning, selector,
NIS, information, window, or decision value.

## Supported inputs

The core fails closed unless the captured evidence represents one-pass Schur,
FEJ enabled, transient `GLOBAL_3D`, `CamRadtan`, fixed camera intrinsics,
fixed camera extrinsics, fixed camera time offset, exactly two configured
stereo cameras, and target-time stereo observations. Fixed calibration IDs
may be `-1`; support is established from frozen configuration plus finite
value shapes, not covariance membership.

P1 reads the following development runs in this fixed order:

1. `07_infinite_fast_on1_post_mh`
2. `10_infinite_on1_post_mh`
3. `04_infinite_head_on1_post_mh`
4. `05_square_head_on1_post_mh`
5. `08_square_fast_on1_post_mh`
6. `11_square_on1_post_mh`

The deterministic repeat scans are `07_infinite_fast_on1_post_mh` and
`05_square_head_on1_post_mh`. Their complete output digests must match their
first scans. Sequence names and trajectory outcomes are unavailable to the
selector and are joined only after admission is frozen.

## Eligible source outcomes

Only exact mappings of these source failures are candidate-eligible:

```text
INIT_ILL_CONDITIONED
INIT_TOO_FAR
INIT_BASELINE_RATIO
SCHUR_RANK_DEFICIENT
SCHUR_ILL_CONDITIONED
```

Unmapped outcomes and every other outcome are ineligible. In particular,
`FULL_NIS_REJECTED`, nonfinite/pathological failures, too-near/behind,
refinement failure, insufficient rows, accepted full factors, and invalid
inputs are never rescued.

## Frozen processing order

For each callback, process canonical candidate and group keys in ascending
order and apply:

```text
typed exact source outcome
-> observation/clone/stereo identity
-> checked CamRadtan inverse and bearing covariance
-> target-time stereo range and d_LCB
-> captured-prior camera-center translation and t_UCB
-> strict acute test
-> worst-axis rho test
-> minimum feature count
-> pilot_consensus_v1 single trim/refit
-> spatial coverage on retained inliers
-> strict rank and conditioning
-> one pre-NIS winner
-> winner-only shadow NIS
-> predicted orientation information
```

Exactly one winner is frozen before NIS. A winner NIS or post-NIS failure
causes coast; no runner-up is evaluated. Every eligible non-winner and its
foregone features/information is logged.

## Frozen windows

```text
E07 analysis:       callbacks 1467--1474 inclusive
E07 primary:        callbacks 1470--1471 inclusive
E10 analysis:       callbacks 1245--1252 inclusive
E10 primary:        callbacks 1248--1249 inclusive
E10 pre-onset:      callbacks 1245--1247 inclusive
```

These are report joins only and cannot affect callback admission. Event onset
cannot move. The future P2 E10 endpoint is the already-frozen diagonal
true-minus-estimate orientation-consistency statistic, reported as peak and
time-integral over the fixed E10 primary and analysis windows, with A2
compared against both A0 and A1.

## Binding decisions

Exactly one P1 decision is issued using the precedence and numeric gates in
`P1_DECISION_PREREGISTRATION.json` and `P1_METRIC_CONTRACT.md`:

```text
P1_GO_T1_INTEGRATION_BOTH_FAMILIES
P1_GO_T1_INTEGRATION_INFINITY_PRIMARY
P1_NO_GO_CERTIFICATE_EMPTY
P1_NO_GO_INFORMATION_NEGLIGIBLE
P1_REVIEW_REQUIRED
```

A failed E07 result cannot amend thresholds, reduce `n`, change runner-up
policy, move a window, or introduce another certificate in P1. Expected E07
sparsity, including the four-distinct-cell requirement for retained n=4
groups, is a scientific result rather than an amendment trigger.

