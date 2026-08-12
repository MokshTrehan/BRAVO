# TurnSafe v6 Session 0 handover

Status: **PASS at the Session 0 hard stop**

Authority: `docs/turnsafe/guidance/IMPLEMENTATION_PLAN_V6.md` Sections 12,
15 (Session 0), and 18

## Identity and authorization

| Field | Value |
|---|---|
| Entry token | `BEGIN_SESSION_0_V6` |
| Authoritative cwd | `/home/moksh/newSlam turnsafe-primary` |
| Branch | `schurvio-lite/turnsafe-primary` |
| Start SHA | `82504db63fafda40dcf44b8e66cbd29609743a1d` |
| Start/frozen tree | `10cf0685f6bf535633f908e5538f1daab536f371` |
| Tracked content checkpoint SHA | `ac38e2f7a7cb07189b7345d41f17b0e8f4c3d6ae` |
| Tracked content checkpoint tree | `4fb7f5c8c564117cfc5be49d0b7f2793b045f133` |
| Final metadata commit | This document's containing commit; exact final HEAD is recorded in `artifacts/turnsafe/session_00/GIT_END.txt` |
| Required next token | `APPROVE_SESSION_1_T0_INSTRUMENTATION` |

The entry gate passed with a clean tracked worktree/index, exact frozen
HEAD/tree equality, and readable authorized Ceres. The complete start capture
is `artifacts/turnsafe/session_00/GIT_START.txt`.

## Session outcome

- A fresh Release ROS1 Noetic build completed with `/usr/bin/python3`, the
  authorized read-only Ceres prefix, testing enabled, and two compile jobs.
- Fresh runtime discovery produced 6 CP1 and 21 CP2 registered executables.
  Direct CP1 passed 35/35 cases, registered CP2 passed 21/21 executables, and
  full CTest passed 27/27 executables.
- Two independent one-pass MH_01 replays produced identical stable
  state/deviation/trajectory/callback-timestamp digest
  `8525ee08a3e7b00261bcd5089cefd6b51c279747ecb26bed9505edc5e890010a`.
- The frozen evo evaluator path completed translation APE and translation/
  rotation RPE. Versions and executable/configuration hashes are frozen in
  `artifacts/turnsafe/baseline_manifest.json`.
- EuRoC is ready. KAIST is classified ENGINEERING-blocked because no public
  recorded input was supplied. Custom development data and the public split
  commitment are also `NOT_SUPPLIED`; no identities or hashes were invented.
- The v6 decision, T0 schema, evaluation, flight, and related-work contracts
  are frozen. Numerical consensus and catastrophic-event thresholds absent
  from the controlling plan remain explicit preregistration fields rather
  than implicit defaults.

Production estimator behavior changed: **no**.

No factor, fallback, decision-changing diagnostic, production threshold, or
T0 instrumentation was implemented in Session 0.

## Tracked files and commits

Commit `74b9c12dee052729161459c8c2211199dafe0a8e` (`docs(turnsafe): freeze v6
session 0 contracts`) adds:

- `docs/turnsafe/decision_log.md`
- `docs/turnsafe/t0_schema.md`
- `docs/turnsafe/evaluation_protocol.md`
- `docs/turnsafe/flight_collection_protocol.md`
- `docs/turnsafe/related_work_matrix.md`

Commit `f30798a12633ebf44cc1bb6c48b48a8f700a5716` (`tools(turnsafe): add
frozen baseline digest`) adds:

- `scripts/turnsafe/baseline_digest.py`
- `scripts/turnsafe/test_baseline_digest.py`

Commit `ac38e2f7a7cb07189b7345d41f17b0e8f4c3d6ae` (`docs(turnsafe): close
consensus threshold gate`) corrects the contract sequencing in:

- `docs/turnsafe/decision_log.md`
- `docs/turnsafe/t0_schema.md`

It requires scientific-authority threshold freeze no later than Session 2,
offline re-evaluation of the captured raw T0 metrics, and a failed T1 gate when
the threshold set remains absent. Session 3 implements but does not select the
set.

This handover is the only additional tracked Session 0 metadata file. All
build, test, replay, data-readiness, and report evidence remains untracked
under `artifacts/turnsafe/`; scratch build state remains under
`.turnsafe-work/session_00/`.

## Commands and tests

The command/outcome ledger is
`artifacts/turnsafe/session_00/COMMANDS.log`; it supplies executable
reproduction blocks and flags the one early discovery argv that was not
preserved. Raw results are summarized in
`artifacts/turnsafe/session_00/TEST_RESULTS.md`.

Green checkpoints:

| Check | Result |
|---|---|
| Fresh Release configure/build and registered test-target build | PASS |
| Direct CP1 runtime cases | 35/35 PASS |
| Registered CP2 executables | 21/21 PASS |
| Full registered CTest executables | 27/27 PASS |
| Digest-tool unit tests | 5/5 PASS |
| Independent frozen replay executions | 2/2 PASS |
| Stable digest comparison | exact PASS |
| evo APE/RPE evaluator commands | 3/3 PASS |
| JSON artifact validation and `git diff --check` | PASS |

The post-diff rerun repeated direct CP1 (35/35), full registered CTest
(27/27, including all 21 CP2 targets), and the digest-tool suite (5/5 plus
two-run digest equality). Its logs are `FINAL_CP1_DIRECT.log`,
`FINAL_CTEST_ALL.log`, and `FINAL_DIGEST_TOOL.log`.

## Failure ledger

| Classification | Event | Disposition |
|---|---|---|
| ENGINEERING | Initial CTest wrapper split the authoritative worktree path at its space; no test binary ran. | Preserved failure log; mechanically quoted only the generated scratch CTest command strings; source unchanged; CP2 21/21 and full CTest 27/27 then passed. |
| ENGINEERING | No public KAIST recorded input was supplied. | Decisive `KAIST_RECORDED_INPUT_NOT_SUPPLIED`; no run claimed and no legacy lane repaired. |
| ENGINEERING | Custom development release and public split commitment were not supplied. | Explicit `NOT_SUPPLIED` status artifacts; no seed, ID, membership, path, or hash invented. |
| CORRECTNESS | One early broad `find` rooted at `/home/moksh/Downloads`; exact argv/output not preserved. | Known result contained only public EuRoC/TUM bag paths and no private/holdout-marked metadata. No exposure was observed; subsequent checks were explicit-path only. The report does not claim a complete reproducible firewall audit. |

No SCIENTIFIC failure occurred. No private manifest, raw holdout surface, or
holdout-marked metadata was observed or used. The procedural deviation limits
the claim to observed workspace-side non-exposure and is not treated as an
external quarantine attestation.

## Key artifact checksums

| Artifact | SHA-256 |
|---|---|
| `baseline_manifest.json` | `515efaf9a29febc4734d7568575c68402e519e652b445bc22ba2ffcd32932d77` |
| `baseline_digest/comparison.json` | `444c4a95373129e36bacde8caf9056989d042cadd4248678dbcb40d8017e1653` |
| `baseline_digest/run_1/digest.json` | `73f68a5798bf84ab4d728a341bd54a6de3239f668ff8efb77de17a710f12b4ab` |
| `baseline_digest/run_2/digest.json` | `14e123c4c741f2b57dd1ebcfcfe2b99629a9eeef378fc42ae436691611948aeb` |
| `data/DATASET_INVENTORY.md` | `3b8eb5cfbf0fb34c428ff844d225c7cdeee8467a7391037ddcbc00edbed4d954` |
| `data/KAIST_COMPATIBILITY_REPORT.md` | `4f652f0b80e3212833cf7da451cb7c6cb42001f3d2634c68c6c6a46a475f2ebd` |
| `session_00/BUILD.log` | `73eab314229f53a20a0820f288dab9717c5d5fbab57262ff60adc6fa63f77a0e` |
| `session_00/CP1_DIRECT.log` | `56e0cb729d49f62f27dd7de286ebc5a687c9d8f5ef37512ae9a713056339bf70` |
| `session_00/CP2_CTEST.log` | `492acac303331db5eeb903f8ebac0c648f73597a715adac959bab18988823d88` |
| `session_00/CP2_CTEST_WORKAROUND.log` | `8c97c8d537e011d7898b1f10ad390f4cd27142459a75bafb163f4e9e48f08252` |
| `session_00/CTEST_ALL.log` | `66ebea87943091ac8e180894573c1e75550f5e911713abbea89986e0714a3578` |
| `session_00/FINAL_CP1_DIRECT.log` | `59b635af58e9c99c356531778da942c95f8bed5ef4036e1646ae3c7cbe2686c9` |
| `session_00/FINAL_CTEST_ALL.log` | `5f12e417539128d3074260e6cbd205c4f3b325081ae4c8ad78984ea139a58446` |
| `session_00/FINAL_DIGEST_TOOL.log` | `7152c00619272c2d941cd4d976f69d8cb15664e5527e8dd4eb93abb5d5672046` |
| `session_00/PROVENANCE_RECHECK.log` | `858258e00194bf48b582d95de7c13ec56b3cffacfff3820a61e6120bfd717928` |
| `session_00/GIT_START.txt` | `d283324ed61714bda5e60e69e9fa1e2e84c37f674f25b01a27eca7ae5bbb7d70` |

The final complete artifact checksum list is
`artifacts/turnsafe/session_00/SHA256SUMS`.

## Gate state for the next session

Session 1 may begin only after the exact next token is supplied. Its scope is
passive T0 instrumentation under `turnsafe.t0.v1`; the baseline's unavailable
accepted-ID/lifecycle/callback-decision fields must remain declared gaps until
passively exposed. Consensus threshold-dependent results remain
`THRESHOLD_SET_NOT_FROZEN`. The scientific authority must freeze that set no
later than Session 2, and the raw T0 records must be re-evaluated before the T1
branch gate; an absent set makes that gate fail. No factor rows or decision
changes are authorized by this handover (plan Sections 7.7--7.8 and 15,
Sessions 1--2).
