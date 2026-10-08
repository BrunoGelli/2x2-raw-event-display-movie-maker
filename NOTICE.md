# Source provenance

This project reuses small pinned parts of BrunoGelli/2x2-raw-event-display at
commit `cd4f5632fe42e05ab41cb9bd5202f1fde3c5228a`:

- `raw_movie/_upstream/geometry.py`: unchanged PacMon geometry loader/download helper.
- `raw_movie/_upstream/codec.py`: unchanged legacy wire definitions and decoder utilities.
- `raw_movie/_upstream/timing.py`: unchanged `TimingConfig` and `TimestampUnroller`
  excerpt, ending before `DetectorPlayback`.
- `raw_movie/_upstream/post_sync.py`: unchanged display-policy helper.

The offline HDF5 adapters, epoch validation, external sorting/replay, renderer,
encoder, CLI, and tests are implemented here. No network collector is used.
The live repository did not supply a general project license; this notice does
not assign a new license to that code.

Upstream PacMon attribution, preserved from the live display:

Copyright © 2023 FERMI NATIONAL ACCELERATOR LABORATORY for the benefit of the
DUNE Collaboration.

PacMon geometry is Apache License, Version 2.0. Geometry is downloaded
separately at commit `2cf0e2c7db056dd205efb7f41616c1795fa9ea67`, with Git blob
hashes checked by the explicit fetch command. Its license is available at:
https://github.com/BrunoGelli/2x2Pacmon/blob/2cf0e2c7db056dd205efb7f41616c1795fa9ea67/LICENSE

ASIC rollover arithmetic references DUNE/ndlar_flow commit
`a0eb2f364e35340d67fd73dc09a8e8f847211a58`,
`src/proto_nd_flow/reco/charge/raw_event_builder.py`, as recorded by the live
display. Flow is not a runtime dependency.

File-format and integration sources inspected:

- `lbl-neutrino/arcube_nearline` at `d1726cc6a3d336a20044a85547125ece17da6a83`:
  `actions/packetize.sh`, `watcher.py`, `lib/paths.inc.sh`.
- `larpix/crs_daq`, `2x2-run3-cooldown` at
  `c00bd296c25afb4323008edf009751bee531dc45`: `record_data.py`.
- `larpix/larpix-control` at `5a69050422e82356c8faf9ad0ea3168c322d63e8`:
  `larpix/format/rawhdf5format.py`, `hdf5format.py`, `hdf5format_direct.py`,
  `pacman_msg_format.py`.

The nearline packetizer invokes the direct raw-to-packet converter; it copies
already-packetized recordings. Current larpix-control direct conversion
requires explicit ASIC v2 / format 2.4 arguments, although the inspected
nearline wrapper does not pass these. This tool reads existing recordings
and does not modify that pipeline or invoke its converter.
