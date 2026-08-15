# Phase A readiness inventory

Status: `ACTIVE -- desktop inputs/build/tests verified; evidence registry and Orin blocked`

Recorded: 2026-08-15, America/Toronto

This inventory records local presence before any acquisition. It is evidence
for `EXECUTION_PLAN.md` Phase A and is not itself a science result.

## Source identity

| Field | Value | Status |
|---|---|---|
| Worktree | `/home/moksh/schurvio-lite-icra27-a0` | PASS |
| Branch | `schurvio-lite/icra-robust-core` | PASS |
| Frozen base commit | `4d2f2d275437ead9496b848730d5a9eb0e402864` | PASS |
| Frozen base tree | `7c390aeb126f1ef69aad75733f40559639889ed3` | PASS |
| Frozen local tag | `icra27-a0-base` | PASS |
| Estimator binary built in this worktree | `build/cp0-ws/devel/lib/ov_msckf/ros1_serial_msckf`; SHA-256 `8de5970c9654c1bb5c2fcdc5646fd5dd8675198d4ba41c6bdae8e5edba2b80ed`; ELF build ID `5e2538a1e79e913df3c3e288d083df4f683099e3` | PASS |
| Clean Phase A source snapshot | Commit `ab51413985d8213f40d7124364faf69631da135d`, tree `29c3689adc2f70f51c152f8948fa1e8e1b89207f`; `source_dirty=false`; aggregate source SHA-256 `098b95dc77adc756f6363ce312f88ff8aaba62953c44e7281d5021768e1333ca` | PASS |

The dirty T1 worktree at `/home/moksh/newSlam turnsafe-primary` is not a
campaign input and must remain untouched.

The write-locked source snapshot is stored at
`/home/moksh/schurvio-icra27-artifacts/readiness/source/ab51413/source_snapshot.json`;
the snapshot file SHA-256 is
`bb021b3c208993a86144a43b3e38e40c03af9d7d59efa1cf0d188a89a7bb3f42`.

## Dataset inventory

Fresh full-file hashing was performed before considering downloads.

| Dataset | Local result | Identity result | Acquisition decision |
|---|---:|---:|---|
| EuRoC MAV | 11/11 bags present | 11/11 match tracked SHA-256 values in `project/datasets.yaml` | No download |
| KAIST-VIO source | 11/11 bags present | 11/11 match `BAG_INVENTORY.csv` | No download |
| KAIST-VIO adapted | 11/11 bags present | 11/11 match `BAG_INVENTORY.csv` | No download |
| TUM-VI room4 | Present, 2,346,890,121 bytes | SHA-256 `8027451a7390efd51098a10949b91dae7c183d9daf1a09ab058e8169c55ecf9e` | No download |
| TUM-VI corridor4 | Present, 2,029,099,748 bytes | SHA-256 `a244ed421af33c73aca53b54a7b150d0f538f350e8d8333d072e5e4845d0a955` | No download |
| TUM-VI outdoors4 | Present, 14,736,044,692 bytes | SHA-256 `85b6359d6779771d749cca2489069b52e26ae3bfcfc41ba6d65a306d78a4c97f` | No download |

Dataset paths:

- EuRoC: paths currently declared in `project/datasets.yaml`.
- KAIST: `/home/moksh/datasets/KAIST_VIO`.
- TUM-VI trio: `/home/moksh/Downloads/tum_vi/calibrated/512_16`.

KAIST manifest anchors:

| Artifact | SHA-256 |
|---|---|
| `manifests/BAG_INVENTORY.csv` | `33b91081885d1d0e849bd3b8517cd5bfe2ccf073a8a0d7e2a01a8a89a4869a51` |
| `manifests/DOWNLOAD_MANIFEST.json` | `17502175a17cf6c9fc8c2635dbd41925348632cdfc03d2ef572e5bb25ae72d64` |
| `manifests/SHA256SUMS` | `6720fdf8b9b78f712e255a5cc695c761cb344cba4ed05669e42d6d0524176cfd` |

The tracked TUM-VI registry is stale: it says room1-room5 are pending and does
not register corridor4/outdoors4. EuRoC and TUM ground-truth roots also point
into a legacy worktree. Both issues must be repaired with portable registry
bindings before G0.

## Prior-evidence packaging

| Item | Current result | Required repair |
|---|---|---|
| Runner-required `KAIST_BASELINE_RESULTS.csv` | Exact 126,368-byte import is tracked at `project/evidence/kaist/session_0_5/KAIST_BASELINE_RESULTS.csv`; SHA-256 `99f94546...`; portability tests pass | Resolved; treat as historical input identity, not fresh performance evidence |
| Tracked CP0 verification | Historical package repaired with its exact 27,498-byte build provenance member; the artifact-snapshotted verifier passes against the recorded checksum anchor | Resolved as historical evidence; still not a fresh A0 reproduction |
| `EVIDENCE_SYNTHESIS.md` | External SHA-256 `bd0ee1c6c89c3c38dd89fef403ccf4742b95ee12cbb63791d333f1206a0a2aed` | Register by content hash and provenance |
| `session_a_result.json` | External SHA-256 `e3e717b7d8d8c65b8e3d8dda7c967fbc4c7ed4b5eda9de667e58632b9fff5e66` | Register by content hash and provenance |

The KAIST runner dependency is now available from a fresh worktree. A
prepare-only rotation-fast preflight passed every frozen input binding and
produced `sequence_result.json` with SHA-256
`509b8335e5ddcea31f8bb9f8617e2fb61a1a96422914b1616162337282828d7f`.
The write-locked copy is under the primary artifact root at
`readiness/preflight/kaist-rotation-fast-prepare-20260815`. Science runs remain
blocked on the broader registry-portability and G0 requirements.

## External-system local inventory

| System | Local candidate | Commit | Tracked state | Acquisition decision |
|---|---|---|---|---|
| Upstream OpenVINS | `/home/moksh/schurvio-baseline-triad/20260809T190830Z/external/open_vins` | `69488123ed9362dd44b6f28e7f4680abbff1442b` | Clean | Reuse by hash; no clone |
| ov_SchurVINS | `/home/moksh/schurvio-baseline-triad/20260809T190830Z/external/ov_SchurVINS` | `d988139c0ba3cc51abacb8b5b5acb482a19325ba` | Clean | Reuse by hash; no clone |
| Official SchurVINS | `/home/moksh/schurvio-novelty-audit/20260809T170019Z/repos/SchurVINS` | `d8ab6dff20860694d8d879d716d7fd6cb2c2530e` | Clean | Reuse by hash; no clone |
| ORB-SLAM3 | `/home/moksh/turnsafe-baselines/p0b-4d2f2d2/ORB_SLAM3` | `4452a3c4ab75b1cde34e5505a36ec3f9edcdc4c4` | Dirty tracked tree plus build outputs | Do not reuse as clean source; expanded rerun is low-priority |
| VINS-Fusion dataset fork | `/home/moksh/schurvio-icra27-external/VINS-Fusion-kaist-bb037a6` | `bb037a6fb914b600eee7cb9b5856d2d03607ff6a` | Clean detached checkout; fork has no license file and is not standalone because it omits `camera_models` | Use only as dataset-author configuration/source context |
| VINS-Fusion official | `/home/moksh/schurvio-icra27-external/VINS-Fusion-official-be55a93` | `be55a937a57436548ddfb1bd324bc1e9a9e828e0` | Clean detached checkout; upstream `LICENCE` retained | Primary build source for the timeboxed engagement |

VINS-Fusion was the only confirmed missing mandatory test system and has now
been acquired at the two pinned identities above. Build/bring-up remains the
separate two-working-day Phase D timebox. The two histories have no common
Git commit, so their relationship must not be described as an ordinary fork
without further provenance work.

## Host and capacity

| Item | Observed value | Status |
|---|---|---|
| OS/architecture | Ubuntu 20.04, x86_64 | PASS |
| CPU | AMD Ryzen 9 9950X, 32 logical CPUs | PASS |
| RAM | approximately 98.8 GB | PASS |
| Free storage | 755,247,493,120 bytes; inode use 2% | PASS |
| ROS | Noetic | PASS |
| GCC | 9.4 | PASS |
| CMake/CTest | 3.16.3 | PASS |
| catkin_tools | 0.9.4 | PASS |
| Python | `/usr/bin/python3` 3.8.10 | PASS |
| OpenCV | 4.2 | PASS |
| Eigen | 3.3.7 | PASS |
| evo | 1.31.1 | PASS |
| Docker | 26.1.3 | PASS, but existing Dockerfile is not an immutable paper environment |
| Primary artifact root | `/home/moksh/schurvio-icra27-artifacts`, mode `0750` | PASS |
| Geometry reserve | at least 25 GB required | PASS for capacity; backup target and measured forecast still pending |

The build reuses an already-present, clean Ceres 1.14.0 source at commit
`facb199f3eda902360f9e1d5271372b7e54febe1`; no Ceres network download is
needed.

## Clean build and test result

The exact A0 tree built successfully from the clean worktree with the pinned
local Ceres source. All five catkin packages (`ov_core`, `ov_data`, `ov_eval`,
`ov_init`, and `ov_msckf`) built. The only compiler diagnostics were existing
unused-variable warnings in `ov_eval` and the existing Ceres miniglog warning.

| Check | Result | Evidence |
|---|---:|---|
| Production ROS1 build | PASS | `scripts/cp0/build_ros1.sh`; all 5 packages built |
| Build provenance | PASS | `build/cp0-ws/CP0_BUILD_PROVENANCE.json`; SHA-256 `87b88fbe67273153f3a5c079a8d8f2e9a9b98fbce789b6f35e242af1e6949e0c` |
| Registered catkin tests | PASS | all 5 packages; `ov_msckf` 580 tests, 0 errors, 0 failures, 0 skipped |
| TurnSafe Python tests | PASS | 123 tests after adding the evidence-registry checks |
| CP2 Python tests | PASS | 494 tests, each test module run in its required fresh `/usr/bin/python3 -I -B` process |

Running all CP2 modules in one interpreter is intentionally invalid: a
protecting test rejects preloaded postauthorization modules. The first broad
discovery attempt exposed that guard; rerunning every module in a fresh
isolated process passed. This is a runner-contract diagnosis, not a waived
test failure.

## Orin readiness

Status: `BLOCKED`

The target is now confirmed by the user as an NVIDIA Jetson Orin Nano
Developer Kit, and the dependency registry has been corrected. Its module
memory/SKU, JetPack/L4T image, storage/cooling setup, SSH endpoint, usable-core
policy, power interface, `tegrastats`, and `nvpmodel` evidence remain unknown.
`project/d0_status.py --strict` therefore still fails on the unresolved
target-board requirements. No connection attempt has been made because no
endpoint is yet available.

Desktop Phase A may continue. H3/embedded work cannot pass G0 until an Orin
endpoint and fixed profile/power proof are supplied. If unavailable within 48
hours, the plan requires an explicit embedded-claim cut rather than waiting
until the Orin campaign.

## Download ledger

| Timestamp | Item | Local-first result | Action | Pinned identity |
|---|---|---|---|---|
| 2026-08-15 | EuRoC/KAIST/TUM-VI | Present and verified | No download | Identities above |
| 2026-08-15 | Ceres 1.14.0 | Present and clean | Local source reuse; no network download | `facb199f3eda902360f9e1d5271372b7e54febe1` |
| 2026-08-15 | VINS-Fusion | No local checkout found | Downloaded clean detached dataset-author and official checkouts; no build attempted | Dataset fork `bb037a6fb914b600eee7cb9b5856d2d03607ff6a`; official `be55a937a57436548ddfb1bd324bc1e9a9e828e0` |
| 2026-08-15 | KAIST Session-0.5 ledger | Present only in an ignored legacy artifact path | Imported exact bytes into the tracked evidence registry and a read-only content-addressed artifact copy; no network action | SHA-256 `99f94546bfe1d807af04c47df82ff5bd0ed63d8c1eb500e5e10bed3fa95bf8b5` |
