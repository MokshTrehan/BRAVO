# ORIN-SWEEP-1 RUN_LOG (desktop, append-only; the board keeps its own per-cell RUN_LOG.md inside the artifact root)

- 2026-08-20T14:06Z Session start. Prereg `d4d3134` verified committed. Branch `orin/sweep-20260820` created from `d4d3134`.
- 2026-08-20T14:06Z Board pre-flight: all checks pass (STEP0_TABLE.md §Pre-flight). Binary `b9f4f034…` re-hashed OK.
- 2026-08-20T14:15Z C5 energy convention reproduced from raw C5 logs (DECISIONS D6).
- 2026-08-20T14:25Z Tooling written: sweep launches (2-arg deltas), C5 runner byte copy, host driver, verifier, thermal sampler, queue generator. Step-0 table + DECISIONS D1–D8 committed before any run.
- 2026-08-20T14:15Z Board root `/data/orin-sweep-20260820T141547Z` created; fresh 60 s idle baseline captured; driver started detached (pid 91014).
- 2026-08-20T14:22Z INTEGRITY GATE PASS: gate-kaist-rotation_fast-C5 and gate-euroc-MH_05-C5 byte-identical to C5 references (state_estimate + state_deviation, 4/4 IDENTICAL). Gate timing: KAIST p50 45.41 ms / p99 91.90 ms / compliance 10.4 %; EuRoC p50 53.19 / p99 115.88 / 39.2 %. Max cpu 43.6 °C, no throttle.
- 2026-08-20T14:24Z Phases a, b1, b2, c (144 cells) appended to QUEUE.tsv in prereg order (repeat-major within phase, D5). PREREG NOW FROZEN (first sweep run = kaist-rotation_fast-S1-b200-r1).
- 2026-08-20T14:26Z First sweep cell kaist-rotation_fast-S1-b200-r1 (ROS-override path) byte-identical to the C5 reference (`72236fee…`): override ≡ YAML confirmed. N0 cell ran (nullspace resolved, passes=1).
- 2026-08-20T14:41Z D9 recorded (cold bag reads inflate the energy window: wall−Σlatency 66 s / 33 s on the first circle cells vs 14–16 s steady). STOP inserted after kaist-circle-S1-b100-r1 (cursor 9).
- 2026-08-20T14:42:29Z Driver exited cleanly at STOP (DRIVER_COMPLETE, 9 sweep cells + 2 gate cells done). STOP removed; D9 driver (sha 2eae4689…) deployed; restarted 14:42:34Z from cursor 9 at kaist-circle-N0-b100-r1 (bag_warm_s=0.8).
- 2026-08-20T14:43Z Early descriptive reads (r1 only, not verdicts): rotation_fast compliance 11 % at F, 89 % at 100 (both modes); circle 1 % at F, 55 % at 100 (S1); ATE rotation_fast 0.090 m (F) → 0.572 m (100) — accuracy cliff below F; S1/N0 ATE identical to 1e-7 at F on both sequences.
