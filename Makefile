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
	$(PY) scripts/icra27/paper_numbers_check.py $(GOLDEN) $(OUT)/GOLDEN.sha256.json
