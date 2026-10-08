"""PacMon JSON -> physical tile metadata + dense channel-to-pixel lookup.

Preserves the PacMon plot.go convention:
 module=(iog-1)//2; geo_iog=2-iog%2;
 geo_tile=(io_channel-1)//4+1 + 8*(1-iog%2).
All four Hydra IO channels of a tile map to the same physical pixels.
"""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import urllib.request
import numpy as np

PACMON_COMMIT = "2cf0e2c7db056dd205efb7f41616c1795fa9ea67"
GEOMETRY_BLOBS = ["c2e030bfb7d30b14b9d060acd0b830f17df8e714",
                  "73ba6caa22622dc04e4cb092901b0b3879d80a61",
                  "3ad1b36b4a9de2b5523520ea2b5f895f36fb987f",
                  "afb4a3a9881909bc25a730765c4df944f026339a"]
PIXEL = np.dtype([("col", "<u2"), ("row", "<u2"), ("chip", "u1"),
                  ("channel", "u1"), ("iog", "u1"), ("tile", "u1")])


@dataclass
class Geometry:
    lut: np.ndarray
    pixels: np.ndarray
    metadata: dict

    def lookup(self, iog, hits):
        # Invalid IO channel 0 or >32 is unmapped, never clipped to a valid pixel.
        ids = np.full(len(hits.io_channel), -1, dtype=np.int32)
        valid = (hits.io_channel >= 1) & (hits.io_channel <= 32)
        ids[valid] = self.lut[iog, hits.io_channel[valid], hits.chip[valid], hits.channel[valid]]
        return ids


def build_geometry(modules, iogs, provenance=None):
    """Build from PacMon GeoConfig dicts; only the requested IO groups are exposed."""
    iogs = sorted(set(iogs))
    if not iogs or any(i not in range(1, 9) for i in iogs):
        raise ValueError("2x2 IO groups must be in 1..8")
    lut = np.full((9, 33, 256, 64), -1, dtype=np.int32)
    parts, tiles, offset = [], [], 0
    for iog in iogs:
        module = (iog - 1) // 2
        config = modules[module]
        pitch = float(config["pixel_pitch"])
        if not np.isfinite(pitch) or pitch <= 0:
            raise ValueError("pixel_pitch must be finite and positive")
        by_tile = {t: [] for t in range(1, 9)}
        for key, xy in config["geometry"].items():
            gi, gt, chip, channel = map(int, key.split("-"))
            if gi != 2 - iog % 2:
                continue
            tile = gt - 8 * (1 - iog % 2)
            if tile not in by_tile or not 0 <= chip <= 255 or not 0 <= channel <= 63:
                raise ValueError("invalid PacMon geometry key: " + key)
            x, y = map(float, xy)
            if not np.all(np.isfinite([x, y])):
                raise ValueError("nonfinite coordinate: " + key)
            by_tile[tile].append((chip, channel, x, y))
        for tile, values in by_tile.items():
            if not values:
                raise ValueError(f"missing geometry for IOG {iog}, tile {tile}")
            values.sort()
            a = np.asarray(values, dtype=np.float64)
            chip, ch = a[:, 0].astype(int), a[:, 1].astype(int)
            x0, y0 = a[:, 2].min(), a[:, 3].min()
            grid = (a[:, 2:4] - [x0, y0]) / pitch
            rounded = np.rint(grid)
            if not np.allclose(grid, rounded, atol=1e-3, rtol=0):
                raise ValueError(f"IOG {iog} tile {tile}: coordinates are not on a pixel-pitch grid")
            col, row = rounded[:, 0].astype(int), rounded[:, 1].astype(int)
            width, height = int(col.max()) + 1, int(row.max()) + 1
            if width * height > 4_000_000 or max(width, height) > 65535:
                raise ValueError("unreasonably large tile raster")
            if len(np.unique(row * width + col)) != len(values):
                raise ValueError(f"overlapping physical pixels in IOG {iog} tile {tile}")
            rec = np.empty(len(values), dtype=PIXEL)
            rec["col"], rec["row"], rec["chip"], rec["channel"] = col, row, chip, ch
            rec["iog"], rec["tile"] = iog, tile
            ids = np.arange(offset, offset + len(values), dtype=np.int32)
            for io in range((tile - 1) * 4 + 1, tile * 4 + 1):
                lut[iog, io, chip, ch] = ids
            tiles.append(dict(iog=iog, module=module, tile=tile,
                              geometry_tile=tile + 8 * (1 - iog % 2),
                              start=offset, count=len(values), width=width, height=height,
                              x_min=float(x0), y_min=float(y0), pitch=pitch))
            parts.append(rec)
            offset += len(values)
    pixels = np.concatenate(parts)
    meta = dict(version=1, record_bytes=8, n_pixels=offset, tiles=tiles,
                iogs=iogs, coordinate_units="mm", provenance=provenance or {})
    meta["geometry_id"] = hashlib.sha256(pixels.tobytes() + json.dumps(meta, sort_keys=True).encode()).hexdigest()
    return Geometry(lut, pixels, meta)


def load_geometry(directory, iogs):
    directory = Path(directory).expanduser()
    modules, provenance = {}, {}
    for module in sorted({(i - 1) // 2 for i in iogs}):
        p = directory / f"geometry_mod{module}_v4.json"
        raw = p.read_bytes()
        modules[module] = json.loads(raw)
        provenance[p.name] = hashlib.sha256(raw).hexdigest()
    return build_geometry(modules, iogs, provenance)


def download_geometry(directory):
    """Explicit, one-time download. Never called by a live collector or web request."""
    directory = Path(directory).expanduser()
    directory.mkdir(parents=True, exist_ok=True)
    for m, expected in enumerate(GEOMETRY_BLOBS):
        name = f"geometry_mod{m}_v4.json"
        p = directory / name
        if p.exists():
            raw = p.read_bytes()
        else:
            url = f"https://raw.githubusercontent.com/BrunoGelli/2x2Pacmon/{PACMON_COMMIT}/layout/{name}"
            with urllib.request.urlopen(url, timeout=60) as response:
                raw = response.read(10_000_001)
        digest = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
        if digest != expected:
            raise ValueError(f"{name}: does not match pinned PacMon blob; use a separate directory for custom layouts")
        json.loads(raw)
        if not p.exists():
            tmp = p.with_suffix(".json.tmp")
            tmp.write_bytes(raw)
            tmp.replace(p)
        print(f"Verified {p} ({len(raw):,} bytes)")


def demo_geometry(iogs=range(1, 9), chips_per_tile=100):
    """Synthetic geometry, conspicuously labeled DEMO. Never a live fallback."""
    modules = {}
    for m in range(4):
        coords = {}
        for side in (1, 2):
            for tile in range(1, 9):
                for c in range(chips_per_tile):
                    chip = c + 11
                    for channel in range(64):
                        x = ((tile - 1) % 2 * 84 + c % 10 * 8 + channel % 8) * 3.8
                        y = ((tile - 1) // 2 * 84 + c // 10 * 8 + channel // 8) * 3.8
                        coords[f"{side}-{tile+8*(side-1)}-{chip}-{channel}"] = [x, y]
        modules[m] = dict(pixel_pitch=3.8, geometry=coords)
    return build_geometry(modules, iogs, {"SYNTHETIC": "not the detector geometry"})
