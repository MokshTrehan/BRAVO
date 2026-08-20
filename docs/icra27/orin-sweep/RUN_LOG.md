# ORIN-SWEEP-1 RUN_LOG (desktop, append-only; the board keeps its own per-cell RUN_LOG.md inside the artifact root)

- 2026-08-20T14:06Z Session start. Prereg `d4d3134` verified committed. Branch `orin/sweep-20260820` created from `d4d3134`.
- 2026-08-20T14:06Z Board pre-flight: all checks pass (STEP0_TABLE.md §Pre-flight). Binary `b9f4f034…` re-hashed OK.
- 2026-08-20T14:15Z C5 energy convention reproduced from raw C5 logs (DECISIONS D6).
- 2026-08-20T14:25Z Tooling written: sweep launches (2-arg deltas), C5 runner byte copy, host driver, verifier, thermal sampler, queue generator. Step-0 table + DECISIONS D1–D8 committed before any run.
- 2026-08-20T14:15Z Board root `/data/orin-sweep-20260820T141547Z` created; fresh 60 s idle baseline captured; driver started detached (pid 91014).
- 2026-08-20T14:22Z INTEGRITY GATE PASS: gate-kaist-rotation_fast-C5 and gate-euroc-MH_05-C5 byte-identical to C5 references (state_estimate + state_deviation, 4/4 IDENTICAL). Gate timing: KAIST p50 45.41 ms / p99 91.90 ms / compliance 10.4 %; EuRoC p50 53.19 / p99 115.88 / 39.2 %. Max cpu 43.6 °C, no throttle.
- 2026-08-20T14:24Z Phases a, b1, b2, c (144 cells) appended to QUEUE.tsv in prereg order (repeat-major within phase, D5). PREREG NOW FROZEN (first sweep run = kaist-rotation_fast-S1-b200-r1).
