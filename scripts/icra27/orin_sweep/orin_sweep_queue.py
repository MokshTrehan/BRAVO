#!/usr/bin/env python3
"""Emit ORIN-SWEEP-1 queue lines (TSV) for a phase. Cell naming: {family}-{seq}-{mode}-b{budget}-r{rep}.
Phases (prereg order, repeat-major inside each phase so r1 of every cell lands first):
  gate   : the two C5 smoke replays (S1, frozen budget, C5 launch, no overrides)
  a      : KAIST x {F,100} x {S1,N0} x r1..r3
  b1     : KAIST x {75,150} x r1..r3        b2 : KAIST x {50,300} x r1..r3
  c      : EuRoC MH_05 x full grid x r1..r3
  topup  : --budgets B.. --families .. -> r4,r5 for given budgets (F and the bracketing budgets)
  ext    : KAIST x {35,25} x r1..r3 (only if the prereg extension rule fired; DECISIONS first)
"""
import argparse, sys
F = 200
TOOL = "/data/tooling/sweep"
KAIST = [("rotation_fast", "/data/turnsafe_adapted/rotation/rotation_fast.bag", "0"),
         ("circle", "/data/turnsafe_adapted/circle/circle.bag", "0"),
         ("square_head", "/data/turnsafe_adapted/square/square_head.bag", "0")]
EUROC = [("MH_05", "/data/euroc/machine_hall/MH_05_difficult/MH_05_difficult.bag", "5")]
L = {"kaist": TOOL + "/orin_sweep_kaist.launch", "euroc": TOOL + "/orin_sweep_euroc.launch"}
C5L = {"kaist": "/repo/project/rotation_robustness_serial.launch", "euroc": "/data/tooling/icra27_cross_dataset_s1_serial.launch"}
MODES = ["S1", "N0"]

def cells(family, seqs, budgets, reps, mode_order=None):
    for r in reps:
        modes = (mode_order or {}).get(r, MODES)
        for seq, bag, bs in seqs:
            for b in budgets:
                for m in modes:
                    yield "\t".join([f"{family}-{seq}-{m}-b{b}-r{r}", family, str(b), m, str(r), bag, bs, L[family]])

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("phase"); ap.add_argument("--budgets", default=""); ap.add_argument("--families", default="kaist,euroc"); ap.add_argument("--reps", default="4,5"); ap.add_argument("--mode-order", default="4:N0,S1;5:S1,N0", help="D11: per-repeat mode order")
    a = ap.parse_args(); out = []
    if a.phase == "gate":
        out.append("\t".join(["gate-kaist-rotation_fast-C5", "kaist", "C5", "C5", "gate", KAIST[0][1], "0", C5L["kaist"]]))
        out.append("\t".join(["gate-euroc-MH_05-C5", "euroc", "C5", "C5", "gate", EUROC[0][1], "5", C5L["euroc"]]))
    elif a.phase == "a": out += cells("kaist", KAIST, [F, 100], [1, 2, 3])
    elif a.phase == "b1": out += cells("kaist", KAIST, [75, 150], [1, 2, 3])
    elif a.phase == "b2": out += cells("kaist", KAIST, [50, 300], [1, 2, 3])
    elif a.phase == "c": out += cells("euroc", EUROC, [F, 100, 75, 150, 50, 300], [1, 2, 3])
    elif a.phase == "ext": out += cells("kaist", KAIST, [35, 25], [1, 2, 3])
    elif a.phase == "topup":
        bs = [int(x) for x in a.budgets.split(",")]; reps = [int(x) for x in a.reps.split(",")]
        mo = {int(k): v.split(",") for k, v in (item.split(":") for item in a.mode_order.split(";") if item)}
        for fam in a.families.split(","):
            out += cells(fam, KAIST if fam == "kaist" else EUROC, bs, reps, mo)
    else: sys.exit("unknown phase")
    print("\n".join(out))

if __name__ == "__main__": main()
