#!/usr/bin/env python3
"""Golden-sample regression for `make paper-numbers`: deterministic tables must match the stored hashes."""
import json, sys
golden, fresh = sys.argv[1], sys.argv[2]
g = json.load(open(golden)); n = json.load(open(fresh))
bad = [k for k in g if n.get(k) != g[k]]
print("golden files:", len(g), "mismatch:", bad)
sys.exit(1 if bad else 0)
