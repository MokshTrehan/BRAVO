# BLACKOUT-1 injection-point table (Step 0, committed before any run)

Generated 2026-08-17T13:12:40Z by scripts/icra27/blackout_injection_points.py. Rules: {"arm_A": "view_start + 0.40 x view_duration on cam0 header stamps inside the frozen replay view, snapped to the nearest cam0 frame (ties earlier)", "arm_B": "centre of the minimum-mean-GT-speed 3 s window; centres are cam0 header stamps; window inside [reference first_state_timestamp, view_end] (DECISIONS D2); ties earliest; unconstrained minimum reported alongside", "gt_speed": "|central finite difference| of GT position per sample; window mean over samples in [c-1.5, c+1.5]", "not_runnable": "mask_past_end: t_b + k >= view_end; overlaps_arm_a: arm-B masked interval intersects arm-A masked interval at the same k (D3)"}

| seq | view start (s) | view dur (s) | ref init (+s) | arm A t_A (s) | +s from start | GT |v| at t_A (m/s) | 3 s mean at t_A | arm B t_B (s) | +s from start | GT |v| at t_B (m/s) | 3 s mean at t_B (min) | unconstrained min centre (+s) / mean | same? |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|
| circle/circle.bag | 1598870181.880224 | 158.837 | 57.086 | 1598870245.401569 | 63.521 | 0.3342 | 0.6760 | 1598870242.429971 | 60.550 | 0.0503 | 0.0743 | 10.579 / 0.0011 | False |
| infinite/infinite.bag | 1598871591.153193 | 149.469 | 3.974 | 1598871650.929362 | 59.776 | 0.2844 | 1.8584 | 1598871626.176670 | 35.023 | 0.0006 | 0.0008 | 35.023 / 0.0008 | True |
| square/square.bag | 1599132361.270393 | 160.679 | 2.204 | 1599132425.546915 | 64.277 | 0.2528 | 3.0883 | 1599132366.309393 | 5.039 | 0.0008 | 0.0023 | 5.039 / 0.0023 | True |
| MH_05_difficult | 1403638523.127830 | 108.550 | 16.164 | 1403638566.527829 | 43.400 | 2.2228 | 2.0492 | 1403638629.927830 | 106.800 | 0.0951 | 0.1435 | 13.200 / 0.0028 | False |
| V2_02_medium | 1413393886.005760 | 117.350 | 4.043 | 1413393932.955760 | 46.950 | 0.4988 | 0.7669 | 1413394001.855761 | 115.850 | 0.0040 | 0.0187 | 1.600 / 0.0083 | False |

Per-k runnability (D3): mask_past_end / overlaps_arm_a

| seq | arm | k=2 | k=5 | k=10 | k=15 | k=20 |
|---|---|---|---|---|---|---|
| circle/circle.bag | A | ok (93.3 s after) | ok (90.3 s after) | ok (85.3 s after) | ok (80.3 s after) | ok (75.3 s after) |
| circle/circle.bag | B | ok (96.3 s after) | NOT_RUNNABLE OVERLAP | NOT_RUNNABLE OVERLAP | NOT_RUNNABLE OVERLAP | NOT_RUNNABLE OVERLAP |
| infinite/infinite.bag | A | ok (87.7 s after) | ok (84.7 s after) | ok (79.7 s after) | ok (74.7 s after) | ok (69.7 s after) |
| infinite/infinite.bag | B | ok (112.4 s after) | ok (109.4 s after) | ok (104.4 s after) | ok (99.4 s after) | ok (94.4 s after) |
| square/square.bag | A | ok (94.4 s after) | ok (91.4 s after) | ok (86.4 s after) | ok (81.4 s after) | ok (76.4 s after) |
| square/square.bag | B | ok (153.6 s after) | ok (150.6 s after) | ok (145.6 s after) | ok (140.6 s after) | ok (135.6 s after) |
| MH_05_difficult | A | ok (63.1 s after) | ok (60.1 s after) | ok (55.1 s after) | ok (50.1 s after) | ok (45.1 s after) |
| MH_05_difficult | B | NOT_RUNNABLE PAST_END | NOT_RUNNABLE PAST_END | NOT_RUNNABLE PAST_END | NOT_RUNNABLE PAST_END | NOT_RUNNABLE PAST_END |
| V2_02_medium | A | ok (68.4 s after) | ok (65.4 s after) | ok (60.4 s after) | ok (55.4 s after) | ok (50.4 s after) |
| V2_02_medium | B | NOT_RUNNABLE PAST_END | NOT_RUNNABLE PAST_END | NOT_RUNNABLE PAST_END | NOT_RUNNABLE PAST_END | NOT_RUNNABLE PAST_END |
