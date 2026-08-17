# BLACKOUT-1 artifact pointer

- Artifact root: `/home/moksh/schurvio-icra27-artifacts/blackout-campaign/blackout1-20260817T130445Z`
- Morning summary: `/home/moksh/schurvio-icra27-artifacts/blackout-campaign/blackout1-20260817T130445Z/MORNING_SUMMARY.md`
- Final aggregate: `/home/moksh/schurvio-icra27-artifacts/blackout-campaign/blackout1-20260817T130445Z/aggregate/final` (aggregate.json, cells.csv, REPORT.md, SHA256SUMS)
- Prereg executed: docs/icra27/BLACKOUT_PREREG.md (amended, git 0d2cd8e, unedited after the first injected run 13:38:29Z); Step-0 table docs/icra27/BLACKOUT_INJECTION_POINTS.md (git d86f0ea)
- Verdicts (mechanical, denominators in the summary): B3 TRUE (inertness + 3 replicas byte-identical); B5 TRUE (0 false commits of 11; 16/16 non-recoveries typed, state preserved); B1' arm A COUNTER-EVIDENCE (5/15 arm-A recON cells recovered, non-monotone in k); B1' arm B 6/11 recovered, K(KAIST) = 2 s across tested sequences (per sequence circle 2, infinite 5, square 5); B2' frozen TRUE 9/9 but 0/9 survive the quantization-aware reading (all rounding-only, D13); B4 15 recON ceilings listed.
- Not runnable (recorded with reasons): 38 of 109 (EuRoC recON 20: frozen binary refuses the single-delta study config, D12; arm-B mask past end 10; arm-A/B overlap 8).
- Tooling: branch blackout/preg-20260817; campaign pinned at 579b2a8 (step0/integrity start) then ffa128c/523bc4f (re-entries after driver-side stops, no cell affected); aggregator/renderer committed after the campaign.
