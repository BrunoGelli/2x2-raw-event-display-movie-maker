#!/usr/bin/env bash
# Run this as an arcube_nearline watcher action, one completed file at a time.
# Export the settings in the FireWorks worker environment, not just the watcher.
set -euo pipefail
: "${RAW_MOVIE_INPUT_ROOT:?Set the absolute raw OR packet stage root}"
: "${RAW_MOVIE_OUTPUT_ROOT:?Set the absolute movie output root}"
: "${RAW_MOVIE_GEOMETRY:?Set the geometry directory}"
: "${RAW_MOVIE_WORK_DIR:?Set the scratch parent for temporary hit runs}"
movie_bin=${RAW_MOVIE_BIN:-raw-movie}
input=${1:?Pass one closed HDF5 file}
shift

# pathlib validates containment and preserves subdirectories, including spaces.
relative_parent=$(python - "$input" "$RAW_MOVIE_INPUT_ROOT" <<'PY'
import sys
from pathlib import Path
source, root = map(lambda x: Path(x).resolve(), sys.argv[1:])
print(source.relative_to(root).parent)
PY
)

exec "$movie_bin" render "$input" \
    --geometry-dir "$RAW_MOVIE_GEOMETRY" \
    --output-dir "$RAW_MOVIE_OUTPUT_ROOT/$relative_parent" \
    --work-dir "$RAW_MOVIE_WORK_DIR" \
    --speed "${RAW_MOVIE_SPEED:-1}" --threads "${RAW_MOVIE_THREADS:-4}" \
    --skip-existing "$@"
