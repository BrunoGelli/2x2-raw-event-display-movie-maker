# Validation and limits

Implemented and tested locally with Python 3.12, NumPy 2.3.5, h5py 3.16,
Pillow 12.3, pytest 9.1, and FFmpeg. `python -m pytest -q` is the repeatable
validation entry point. It includes a real H.264 encode, not a mocked encoder.

Coverage includes:

- Independently constructed legacy PACMAN messages and equivalent packet
  records: identical displayed/highlighted state with chunk sizes 1, 7, 8192.
- SYNC/header state across reads; unequal first PPS times across IO groups;
  previous-cycle receipt correction; out-of-order hits and late triggers.
- Exact trigger start/end boundaries, bad parity, unmapped channels, ignored
  heartbeat words, and configurable post-SYNC veto.
- Pre-SYNC warmup, invalid trigger times, contradictory epoch labels, local
  mode restrictions, empty files, malformed frames, and unsupported formats.
- Physical pixel equivalence for all four Hydra channels of one tile.
- Phosphor decay; real MP4/JSON output; H.264/yuv420p dimensions/frame count
  when ffprobe is available; overwrite protection and encoder-failure cleanup.

The synthetic demonstration was rendered to MP4 and a frame visually checked.
An additional compatibility check decoded the same synthetic legacy raw file
using the pinned larpix-control `pacman_msg_format.parse` + `hdf5format.to_file`
and, separately, `hdf5format_direct.to_file_direct` (ASIC 2, format 2.4).
Both official conversion paths produced exactly the same replayed and
highlighted pixel timestamps as direct raw ingestion: 12 selected hits and
6 trigger-window hits in that small fixture. larpix-control is not required
by this package or by the regular 18-test suite.

The four pinned PacMon geometry files were verified against their Git blob
hashes and loaded successfully: **337,600 mapped pixels across 64 tiles**.
This is geometry capacity, not a claim that every channel is active.

No real NERSC recording was available in this development environment. Real
file throughput, PPS/header consistency, trigger timing, and visual agreement
with the running detector must be checked with the acceptance steps in
[nersc.md](nersc.md). Synthetic tests do not establish physics reconstruction
accuracy or operational performance.
