#!/usr/bin/env bash
# Integrity gate verdict: compare gate replays to the C5 references. Usage: orin_sweep_gate_check.sh <root>
root=$1; ref=/data/orin-bringup-20260820T131729Z
out="${root}/INTEGRITY_GATE.txt"; fail=0
{
echo "ORIN-SWEEP-1 integrity gate — $(date -u +%Y-%m-%dT%H:%M:%SZ) — C5 references ${ref}"
for pair in "gate-kaist-rotation_fast-C5:kaist-rotation_fast-a1" "gate-euroc-MH_05-C5:euroc-MH_05-a1"; do
  g=${pair%%:*}; r=${pair##*:}
  for f in trajectory/state_estimate.txt trajectory/state_deviation.txt; do
    a="${root}/cells/${g}/${f}"; b="${ref}/${r}/${f}"
    if cmp -s "$a" "$b"; then echo "IDENTICAL ${g} ${f} $(sha256sum "$a" | cut -d' ' -f1)"; else echo "MISMATCH  ${g} ${f} got=$(sha256sum "$a" 2>/dev/null | cut -d' ' -f1) ref=$(sha256sum "$b" | cut -d' ' -f1)"; fail=1; fi
  done
done
[[ $fail == 0 ]] && echo "GATE_PASS" || echo "GATE_FAIL"
} | tee "${out}"
exit $fail
