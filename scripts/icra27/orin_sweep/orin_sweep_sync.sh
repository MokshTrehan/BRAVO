#!/usr/bin/env bash
# Mirror the board sweep root to the desktop artifacts tree (read-only pull; never pushes).
set -Eeuo pipefail
stamp=${1:?stamp}
src="orin:/data/orin-sweep-${stamp}/"; dst="/home/moksh/schurvio-icra27-artifacts/orin-sweep-${stamp}/"
mkdir -p "${dst}"
rsync -a --exclude 'cells/*/isolated-home' "${src}" "${dst}"
echo "synced -> ${dst} ($(find "${dst}/cells" -maxdepth 1 -mindepth 1 -type d 2>/dev/null | wc -l) cells)"
