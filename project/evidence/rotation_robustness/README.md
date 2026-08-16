# Rotation-outage evidence index

`R2_ARTIFACT_INDEX.csv` binds the eleven selected S1 scored/capture pairs to
the append-only external artifact root:

`/home/moksh/schurvio-icra27-artifacts/rotation-robustness/r2-20260816T050224Z`

Every row records the scored and capture manifest hashes, state/deviation/TUM
hashes, raw geometry-bag identity, geometry-manifest identity, and literal
final active-SLAM point count. The capture trajectory bytes must equal the
paired scored trajectory bytes. The index was revalidated against the live
artifacts before it was committed.

The raw geometry bag is authoritative. Derived SLAM/MSCKF/loop-feature PLYs
are sparse active or transient visualizations, not persistent dense maps.
See [`ROTATION_OUTAGE_ROBUSTNESS_REPORT.md`](../../../docs/icra27/ROTATION_OUTAGE_ROBUSTNESS_REPORT.md)
for the result, exclusions, and claim boundary.
