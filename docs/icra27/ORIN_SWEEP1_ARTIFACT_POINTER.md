# ORIN-SWEEP-1 artifact pointer (ledger F3) — 2026-08-20/21

- Prereg: `docs/icra27/SWEEP_PREREG.md` (`d4d3134`), frozen at the first sweep run 2026-08-20T14:22:54Z.
- Board root: `orin:/data/orin-sweep-20260820T141547Z/` — `cells/` (194 per-cell directories, each with `SHA256SUMS`), `power/`, `RUN_LOG.md` (board copy), `runs.jsonl`, `QUEUE.tsv`, `INTEGRITY_GATE.txt`, `driver.log`, `state/`.
- Desktop mirror: `/home/moksh/schurvio-icra27-artifacts/orin-sweep-20260820T141547Z/` (rsync of the board root; `aggregate/` with `aggregate.json`, `cells_universal.csv`, `tables.md`, `accuracy_work/`, `SHA256SUMS`).
- Docs: `docs/icra27/orin-sweep/` — `STEP0_TABLE.md`, `DECISIONS.md` (D1–D13), `RUN_LOG.md`, `accounting.json`, `MORNING_SUMMARY.md`.
- Tooling: `scripts/icra27/orin_sweep/` (board driver, launches, verifier, sampler, queue generator, aggregator, summary generator, sync).
- Result line: **F3 REFUTED** (CL-1/CL-2/CL-3 all false); B*(KAIST) = 50 both modes, B*(EuRoC) = 100 both modes; 192/192 cells complete, 0 INVALID_THERMAL, 0 truncated.
