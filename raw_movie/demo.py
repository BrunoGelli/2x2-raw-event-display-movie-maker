"""Deterministic synthetic packet file for smoke tests; never detector data."""
from pathlib import Path
import h5py
import numpy as np

DTYPE = np.dtype([('io_group', 'u1'), ('io_channel', 'u1'), ('chip_id', 'u1'),
    ('packet_type', 'u1'), ('valid_parity', 'u1'), ('channel_id', 'u1'),
    ('timestamp', '<u8'), ('dataword', 'u1'), ('trigger_type', 'u1'),
    ('receipt_timestamp', '<u4')])


def make_demo(path, geometry, seconds=6):
    rng = np.random.default_rng(42)
    rows = []
    base = 1_790_000_000
    for second in range(seconds):
        for iog in geometry.metadata['iogs']:
            rows.append((iog, 0, 0, 4, 0, 0, base + second, 0, 0, 0))
            rows.append((iog, 0, 0, 6, 0, 0, 10_000_000, 0, 83, 0))
            pixels = geometry.pixels[geometry.pixels['iog'] == iog]
            stamps = rng.integers(10, 9_990_000, 900)
            chosen = pixels[rng.integers(0, len(pixels), len(stamps))]
            group = []
            for tick, pixel in zip(stamps, chosen):
                group.append((iog, (int(pixel['tile']) - 1) * 4 + 1, pixel['chip'], 0, 1,
                              pixel['channel'], tick, 90, 0, tick + 10))
            # A track on all planes, coincident with a Light trigger on IOG6.
            track = pixels[(pixels['col'] % 70) == (pixels['row'] % 70)]
            for pixel in track:
                tick = 5_000_000 + int(pixel['row'] % 100) * 10
                group.append((iog, (int(pixel['tile']) - 1) * 4 + 1, pixel['chip'], 0, 1,
                              pixel['channel'], tick, 180, 0, tick + 10))
            if iog == 6:
                group.append((iog, 0, 0, 7, 0, 0, 5_000_000, 0, 2, 0))
            group.sort(key=lambda x: x[6])
            rows.extend(group)
    with h5py.File(path, 'w') as f:
        f.create_group('_header').attrs['version'] = '2.4'
        f.create_dataset('packets', data=np.array(rows, dtype=DTYPE))
    return Path(path)
