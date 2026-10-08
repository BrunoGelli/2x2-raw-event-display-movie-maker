# 2×2 raw event display movie maker

Turn a **closed raw or packetized LArPix HDF5 file into a continuous phosphor
movie**, one MP4 per file. This is the offline counterpart of
[2x2-raw-event-display](https://github.com/BrunoGelli/2x2-raw-event-display),
intended for NERSC batch/nearline use. It displays charge activity on the eight
anode planes grouped into four modules, without Flow event building.

**Version 0.1.0 supports ASIC v2, legacy 16-byte PACMAN words, raw HDF5 0.0,
and packet HDF5 2.3/2.4.** Packetized input is recommended when it already
exists. ASIC v3/new24 and Flow files are not supported. Raw files usually do
not identify the ASIC type: the caller must supply v2 recordings. An
undetectably mislabelled v3 payload cannot be identified from v2 bits alone.

## Install on NERSC

Use a separate Python 3.10+ environment. No GUI, browser, GPU, web server,
ZMQ subscription, or running DAQ is needed.

```bash
git clone https://github.com/BrunoGelli/2x2-raw-event-display-movie-maker.git
cd 2x2-raw-event-display-movie-maker
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test]'
python -m pytest -q

# Once, on a node with outbound access:
raw-movie fetch-geometry --geometry-dir layout
```

Alternatively, copy `geometry_mod{0,1,2,3}_v4.json` from the live display's
PacMon layout directory to `layout/`. The same per-module tile swaps, pixel
coordinates, and Hydra-channel mapping are used. Normal rendering never
downloads files. Custom compatible PacMon layouts are accepted and their
SHA-256 hashes are recorded in each movie's JSON report.

FFmpeg with `libx264` is required. The CLI uses system `ffmpeg` if available,
otherwise the binary installed by `imageio-ffmpeg`. Set `--ffmpeg /path/to/ffmpeg`
or `IMAGEIO_FFMPEG_EXE` to choose explicitly. Fetch geometry and install
dependencies before running on compute nodes.

## First movie

```bash
raw-movie inspect /path/to/packet-run.h5

# Start with a short preview; duration/start are in detector seconds.
raw-movie render /path/to/packet-run.h5 \
    --geometry-dir layout --output-dir movies/preview \
    --duration 10 --width 1280 --height 720

# Full recording: default 1x speed, 30 FPS, 1920x1080.
raw-movie render /path/to/packet-run.h5 \
    --geometry-dir layout --output-dir movies

# The same command accepts the original binary/raw HDF5 file.
raw-movie render /path/to/binary-run.h5 \
    --geometry-dir layout --output-dir movies
```

Output is `movies/<input-stem>.mp4` plus `<input-stem>.json`. MP4 uses H.264,
`yuv420p`, and `faststart` for broad playback compatibility. Files are finalized
only after the encoder succeeds. An output lock prevents concurrent jobs from
writing the same movie. Outputs are never overwritten unless `--overwrite`
is supplied. Interruptions remove temporary output; a killed process may leave
a `.mp4.lock` that must be checked and removed if stale.

The JSON records input size/mtime, schema metadata, geometry hashes, CLI
settings, software version, per-IOG timing/selection counters, trigger counts,
replayed/matched hits, frames, and output duration. These counters expose what
was excluded; this is an activity visualization, not a lossless data archive.

## Playback and triggers

```bash
# Three minutes of detector time become about 30 seconds, plus a 1 s fade tail.
raw-movie render /path/to/packet-run.h5 --speed 6 --output-dir movies/fast

# Yellow detector-wide candidates following recorded beam/light triggers.
raw-movie render /path/to/packet-run.h5 \
    --mode highlight --trigger both --post-us 190 --output-dir movies/triggered

# Only the trigger-window layer (same continuous timeline, including gaps).
raw-movie render /path/to/packet-run.h5 \
    --mode window --trigger beam --output-dir movies/beam

# Match the live 2D viewer's historical 300 us window explicitly.
raw-movie render /path/to/packet-run.h5 \
    --mode highlight --trigger light --post-us 300 --output-dir movies/light300
```

| Setting | Default / meaning |
| --- | --- |
| `--mode` | `all`; `highlight` adds yellow candidates; `window` shows candidates only |
| `--trigger` | `none`; choose `beam`, `light`, or `both` |
| `--beam-iog` / `--light-iog` | 5 / 6; source identity follows the IO group, any T-word subtype |
| `--post-us` | 190 µs; half-open window `[t0, t0 + post)` |
| `--speed` | 1 detector second per movie second; values below 1 slow playback |
| `--decay` | 0.35 **movie** seconds, exponential time constant |
| `--tail` | 1 movie second after the selected interval for the last hits to fade |
| `--start` / `--duration` | Detector seconds relative to the reconstructed file timeline origin |
| `--min-raw-timestamp` | 10 ASIC ticks; set 0 to disable the live display's post-SYNC noise veto |
| `--iogs` | `1,2,3,4,5,6,7,8`; include the trigger source when highlighting |
| `--timezone` | `America/Chicago`; e.g. `UTC` also supported |

Triggers are **temporal candidates across all selected planes**, not proof of
one interaction. Late triggers are matched offline even if their charge hits
arrived earlier. There is no clustering, charge calibration, hot-pixel mask,
Flow selection, or 3D drift reconstruction. ADC does not determine brightness;
each pixel's latest hit time does. Older trigger hits can persist in their
own fading layer. Multiple physical pixels sharing an output pixel use maximum
brightness so that downsampling does not discard isolated activity.

## Timing and visible selections

The implementation retains the live display's 100 ns ticks, 10,000,000-tick
reset period, SYNC subtype **83**, receipt-time boundary correction, and odd
parity policy. Subtype 72 is not treated as the rollover reset. Wire/packet
order is preserved for unrolling; only the resulting timestamps are sorted.
Repeated pixels and out-of-order arrivals are handled deterministically.

Default `--time-mode pps` associates each IOG with a common whole-second epoch
using at least two consistent SYNC/header labels. The **whole file** is checked
before rendering. Missing, duplicate, conflicting, or invalid labels fail
explicitly. Unlike a live display, no stale wall-clock timeout applies to an
offline recording. A PPS/header label is an estimate and can inherit stable
packaging/NTP offsets; it is not a measured cross-IOG phase calibration.

For a short/problematic file, independent local clocks are available:

```bash
raw-movie render /path/to/packet-run.h5 \
    --time-mode local --output-dir movies/unaligned
```

This visibly labels the movie **UNALIGNED**, places each IOG's first valid SYNC
at local zero, and prohibits detector-wide trigger highlighting. It still
requires a valid SYNC on each active IOG. Do not use it to infer simultaneity
between planes. Correct a genuine timing problem before producing aligned
movies; there is no special IOG6 offset.

Display exclusions, with counters in JSON:

- Charge packets failing odd parity; FIFO-diagnostic packet records.
- Unmapped electronics addresses; non-charge configuration/metadata packets.
- Hits before the first valid SYNC in each IOG, commonly up to about one second
  at a file boundary. Each file is independent; clock state is not carried
  from a preceding file.
- Hits rejected by the live unroller's timestamp/receipt consistency checks.
- Raw timestamps below 10 ticks by default (configurable; not a modulo cut).
- Invalid/pre-SYNC external trigger timestamps are counted, not projected.

An endpoint frame includes the final hits. A movie normally lasts
`selected_detector_seconds / speed + tail`, rounded to frame boundaries plus
one endpoint frame. `--start` keeps the fading history before the crop, and
`--duration` crops rendering but **still reads the entire file** for timing and
late-arrival recovery. A view with no usable mapped hits fails instead of
silently creating an empty movie.

## Many files and nearline use

```bash
raw-movie render '/path/to/packet-*.h5' \
    --output-dir movies --skip-existing --continue-on-error

raw-movie render /path/to/packet/directory --recursive \
    --output-dir movies --work-dir "$SCRATCH/raw-movie-work"
```

One file is processed at a time. Duplicate basenames in one batch are rejected
to prevent output collisions; the nearline wrapper below preserves subfolders.
`--skip-existing` means both the MP4 and JSON already exist; it does not verify
that settings or inputs are unchanged. Use a new output directory or
`--overwrite` after changing settings. `--continue-on-error` finishes the other
files and returns a nonzero exit code if any failed.

Hit runs use approximately **12 bytes per selected hit**, plus small `.npy`
headers, on temporary disk. A 100-million-hit file needs roughly 1.2 GB of
temporary storage. Input reads, per-IOG sort buffers, and frame state are
bounded independently of file length; sorted runs are memory-mapped during
replay. `--run-hits` and `--chunk-rows` tune those buffers. Raw chunk rows are
whole messages and their byte size varies. Memory-mapped pages may appear in
RSS but are reclaimable. Use suitable scratch space and account for additional
MP4 space. Temporary runs are deleted when that file completes or fails.

Use allocated CPU resources for production batches. The scripts are examples,
not installed services:

- [NERSC/nearline guide](docs/nersc.md)
- [Nearline action wrapper](examples/nearline_movie.sh)
- [Slurm batch template](examples/render.sbatch)

## Test without detector data

```bash
raw-movie demo --output demo.mp4 --mode highlight --trigger light \
    --width 1280 --height 720 --fps 15
```

This uses synthetic geometry, activity, and triggers and labels every frame
accordingly. It checks the entire HDF5 → timing → MP4 path. It does **not**
validate a real detector recording. See [validation](docs/validation.md) and
[source provenance](NOTICE.md).
