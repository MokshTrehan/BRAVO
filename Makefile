# SchurVIO-Lite ICRA27 — unified paper-number regeneration (C8 Item 3).
# `make paper-numbers`        regenerate every table/figure datum from the raw campaign roots
# `make paper-numbers-check`  golden-sample regression: regenerated deterministic tables must match GOLDEN.sha256.json
PY ?= /usr/bin/python3.8
OUT ?= build/paper-numbers
GOLDEN ?= project/evidence/c8/paper_numbers_GOLDEN.sha256.json

.PHONY: paper-numbers paper-numbers-check

paper-numbers:
	$(PY) scripts/icra27/paper_numbers.py --out $(OUT)

paper-numbers-check: paper-numbers
	$(PY) - <<'PYEOF'
	import json,sys
	g=json.load(open("$(GOLDEN)")); n=json.load(open("$(OUT)/GOLDEN.sha256.json"))
	bad=[k for k in g if n.get(k)!=g[k]]
	print("golden files:",len(g),"mismatch:",bad)
	sys.exit(1 if bad else 0)
	PYEOF
