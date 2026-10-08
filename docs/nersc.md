# NERSC and nearline integration

Use the existing packet stage when available. It avoids repeating PACMAN
byte decoding, but does not introduce Flow's event selection. Raw input uses
the same timeline/geometry/rendering path directly.

## First real-file acceptance

1. Install in a separate environment and fetch/copy geometry on a node with
   network access. `raw-movie inspect FILE.h5` prints the detected schema.
2. Use an allocation or the batch template to render a ten-second preview.
3. Inspect the MP4 and JSON counters. Confirm tile orientation against the
   live display, plausible rates, accepted SYNCs, and the expected Beam/Light
   source. The clock is explicitly a PPS/header estimate.
4. Render matching raw and packet versions from the same recording. Compare
   selected hits, timing counters, and triggered hits, allowing normalized row
   counts to differ: packet files include type-4 header rows.
5. Measure wall time, peak memory, temporary disk and MP4 size for one full
   three-minute recording before selecting production worker concurrency.

There is no automatic NERSC deployment or data access. The Slurm example
requires the user's account/QOS/constraint, allocation sizing, environment,
and paths. Its time request is a starting template, not a throughput claim.
Lower resolution, lower FPS, accelerated playback, and shorter render windows
reduce encoding work. Short windows still scan the complete file.

## Existing FireWorks watcher

The inspected `arcube_nearline/watcher.py` accepts an executable action and
passes it a single file path. Its `_priority` is supplied using `--priority`,
with higher priorities running first. Configure a separate watcher for the
movie stage, using the completed **packet output directory** as input:

```bash
# These variables must also exist in the executing FireWorks worker.
export RAW_MOVIE_BIN=/absolute/movie-maker/.venv/bin/raw-movie
export RAW_MOVIE_INPUT_ROOT=/global/cfs/cdirs/dune/www/data/2x2/nearline_run3/packet
export RAW_MOVIE_OUTPUT_ROOT=/absolute/output/movies
export RAW_MOVIE_GEOMETRY=/absolute/movie-maker/layout
export RAW_MOVIE_WORK_DIR="$SCRATCH/raw-movie-work"

python watcher.py /absolute/movie-maker/examples/nearline_movie.sh \
    --path "$RAW_MOVIE_INPUT_ROOT" --min-file-age 120 --priority -10 --dry-run
```

Review the dry-run selection, then remove `--dry-run` to queue work when ready.
Set priorities relative to your existing Packetizer/panel/Flow priorities;
`-10` is only an example. Do not start watchers here until the full-file cost
has been measured. To consume raw files instead, point `RAW_MOVIE_INPUT_ROOT`
and the watcher to the raw stage; do not schedule both formats unless two
videos per recording are intended.

The wrapper preserves paths relative to `RAW_MOVIE_INPUT_ROOT`, preventing
collisions between runs with identical basenames. Outputs must be outside the
watched input tree. It skips a movie only when both its MP4 and JSON are
already present. Changing render settings requires a new output root or an
explicit rerender. The movie maker does not change any existing nearline
actions or priorities.

The inspected watcher builds a shell command from the action and file path
without shell quoting. Use the existing whitespace-free nearline paths for
that watcher. The wrapper and direct CLI themselves accept quoted paths with
spaces.

## Operational limits

- Only process closed, fully transferred recordings. A minimum file age is a
  heuristic, not a completion guarantee. The reader uses HDF5 read-only mode,
  keeps normal locking, and rejects size/mtime changes during ingestion.
- Retain JSON reports beside videos for auditing timing and display cuts.
- Each file starts an independent unroller; its initial pre-SYNC interval is
  counted as warmup and omitted. No invisible carryover is inferred.
- Use `--time-mode local` only for clearly labelled independent-plane
  inspection. Resolve PPS/header inconsistencies for detector-wide triggers.
- Use separate output directories for different view/speed/window settings.
