# KAIST Session-0.5 identity ledger

`KAIST_BASELINE_RESULTS.csv` is an exact-byte import of the generated
Session-0.5 ledger previously stored only under the ignored logical path
`artifacts/turnsafe/data/KAIST_BASELINE_RESULTS.csv`.

Its fixed identity is:

- size: 126,368 bytes;
- SHA-256: `99f94546bfe1d807af04c47df82ff5bd0ed63d8c1eb500e5e10bed3fa95bf8b5`;
- rows: 11, in the frozen KAIST campaign order;
- source commit: `6e289fc2d1e8c86f0930605fc6b234df3cea1e84`;
- source tree: `40d59b7c4a6341dcc7ae1565f386cbbeb607db39`;
- source-identity record SHA-256:
  `1ea6f6c50364ff8eb6248473f996df51a32aa760f88c8f8c79fe2d38bed3995f`.

The campaign runner uses only the sequence and six input SHA-256 columns.
The remaining fields are retained unchanged to preserve the evidence chain.
They include historical local paths and environment details. This file is
internal campaign evidence and must not be included unchanged in an anonymous
export.

This ledger is historical input-identity and comparison context, not a fresh
reproduction and not current performance evidence. A clean A0 run must
generate its own manifests and metrics.

The local KAIST-VIO distribution has inconsistent licensing signals: its
README declares CC BY-NC-SA 3.0 while its bundled `License` file is GPL-3.0.
This generated ledger contains no bag, image, or ground-truth samples, but
KAIST-VIO attribution remains required. Academic use is intended here;
commercial redistribution clearance is not claimed. Resolve the upstream
licensing ambiguity before publishing this ledger outside the project.
