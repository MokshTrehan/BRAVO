# CP2 pre-data incident log

## 2026-08-02T06:14:40.108703Z — broad read-only source search

- Repository commit at discovery: `b90372485cd48106a004dd6fd6c0391d723991fc`.
- After the exact-commit CP2-A/B unit artifact had finalized and been
  independently reverified, a read-only `rg` source search was invoked with
  the broad directory operand `project/` while mapping the remaining live
  integration surface.
- That operand could cause `rg` to open `project/datasets.yaml`. No registry
  entry or dataset path was printed, resolved, copied, hashed, or passed to a
  provider. No bag, ground-truth file, or recorded-data payload was opened.
- This nevertheless violates the frozen pre-data rule because the rule bars
  opening the registry itself before the five entry-point self-tests and the
  exact unit anchor pass.

Impact and disposition:

- The finalized CP2-A/B artifact is unaffected: its complete execution and
  finalization preceded this command, and its verified policy reports zero
  dataset or bag access.
- No CP2-C/D/E readiness barrier or recorded campaign had started, and none is
  credited as passing. CP2-C remains unauthorized and `not_run`.
- All subsequent pre-barrier searches must use explicit allowlisted paths and
  must exclude `project/datasets.yaml`. A protecting self-test must reject a
  broad project-directory scan or any attempted registry/provider access.
- A fresh readiness barrier, five-entry-point self-test result, and exact
  runtime-commit unit artifact remain mandatory before any recorded input is
  accessed.

There is also a contract-consistency question to resolve before implementing
the readiness snapshot: the artifact schema requires a pre-access source hash
over all tracked files while separately deferring any registry read or hash.
The implementation must not choose a permissive interpretation silently.
