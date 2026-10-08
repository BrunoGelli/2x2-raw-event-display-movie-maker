"""Read closed files without importing larpix-control or disabling HDF5 locking.

Both adapters yield the same ordered per-IOG arrays. Packet type 4 carries
PACMAN message Unix seconds; types 6/7 are SYNC/trigger, not charge hits.
"""
from pathlib import Path
import h5py
import numpy as np
from ._upstream.codec import HEADER, WORD, _legacy_header, odd_parity

PACKET = np.dtype([
    ('kind', 'u1'), ('io', 'u1'), ('chip', 'u1'), ('channel', 'u1'),
    ('timestamp', '<i8'), ('receipt', '<i8'), ('subtype', 'u1'),
    ('parity', '?'), ('diagnostics', '?'), ('second', '<i8'),
])
REQUIRED = {'packet_type', 'io_group', 'io_channel', 'chip_id', 'channel_id',
            'timestamp', 'receipt_timestamp', 'trigger_type', 'valid_parity'}


def attr_text(value):
    return value.decode() if isinstance(value, bytes) else str(value)


def inspect_file(path):
    with h5py.File(path, 'r') as f:
        attrs = {k: attr_text(v) for k, v in f.attrs.items()}
        for name in ('meta', '_header'):
            if name in f:
                attrs.update({k: attr_text(v) for k, v in f[name].attrs.items()})
        if 'packets' in f:
            d = f['packets']
            if not isinstance(d, h5py.Dataset) or d.ndim != 1:
                raise ValueError('Expected a flat /packets dataset, not a Flow file')
            missing = REQUIRED - set(d.dtype.names or ())
            if missing:
                raise ValueError(f'Packet schema lacks {sorted(missing)}; use LArPix v2 format 2.3/2.4')
            version = attrs.get('version')
            if version and version not in ('2.3', '2.4'):
                raise ValueError(f'Unsupported packet format {version}; supported: 2.3/2.4 (ASIC v2)')
            # v3 uses 10-bit dataword; never silently truncate/reinterpret it.
            if 'dataword' in d.dtype.names and d.dtype['dataword'].itemsize != 1:
                raise ValueError('ASIC v3 packet data are not supported in this release')
            kind, count = 'packet', len(d)
        elif 'msgs' in f and 'msg_headers' in f:
            d, headers = f['msgs'], f['msg_headers']
            if d.ndim != 1 or len(d) != len(headers):
                raise ValueError('Raw /msgs and /msg_headers lengths differ or are not flat')
            if not {'io_groups', 'io_group'} & set(headers.dtype.names or ()):
                raise ValueError('Raw /msg_headers lacks io_groups')
            if attrs.get('version', '0.0') != '0.0' or attrs.get('io_version', '0.0') != '0.0':
                raise ValueError('Only raw HDF5 0.0 with legacy PACMAN framing is supported')
            kind, count = 'raw', len(d)
        else:
            raise ValueError('Expected /msgs + /msg_headers or /packets; Flow files are not supported')
        if attrs.get('asic_version', '2') not in ('2', '2.0'):
            raise ValueError('Only ASIC v2 data are supported')
        return dict(path=str(Path(path).resolve()), format=kind, rows=count, metadata=attrs)


def _raw_batches(f, chunk_rows):
    key = 'io_groups' if 'io_groups' in f['msg_headers'].dtype.names else 'io_group'
    for start in range(0, len(f['msgs']), chunk_rows):
        msgs = f['msgs'][start:start + chunk_rows]
        iogs = f['msg_headers'][start:start + chunk_rows][key]
        for iog in np.unique(iogs):
            if not 1 <= iog <= 8:
                raise ValueError(f'Invalid raw IO group {iog} at rows starting {start}')
            bodies, seconds = [], []
            for idx in np.flatnonzero(iogs == iog):
                msg = np.asarray(msgs[idx], dtype=np.uint8).tobytes()
                kind, n = _legacy_header(msg)  # fail closed on new24/truncated input
                if kind != b'D':
                    raise ValueError('Raw file contains non-DATA PACMAN envelopes')
                if n:
                    bodies.append(msg[HEADER.size:])
                    seconds.append(np.full(n, HEADER.unpack_from(msg)[1], dtype=np.int64))
            if not bodies:
                continue
            w = np.frombuffer(b''.join(bodies), dtype=WORD)
            out = np.zeros(len(w), PACKET)
            out['kind'] = 255
            data = w['kind'] == ord('D')
            p = w['payload']
            out['kind'][data] = (p[data] & 3).astype(np.uint8)
            out['io'], out['chip'], out['channel'] = w['io'], (p >> 2) & 255, (p >> 10) & 63
            out['timestamp'], out['receipt'] = (p >> 16) & 0x7fffffff, w['receipt']
            out['parity'] = odd_parity(p)
            aux = np.ndarray((len(w),), dtype='<u4', buffer=w, offset=4, strides=(16,))
            for letter, ptype in (('S', 6), ('T', 7)):
                mask = w['kind'] == ord(letter)
                out['kind'][mask], out['timestamp'][mask] = ptype, aux[mask]
                out['subtype'][mask] = w['io'][mask]
            out['second'] = np.concatenate(seconds)
            yield int(iog), out


def _packet_batches(f, chunk_rows):
    last_seconds = {}
    for start in range(0, len(f['packets']), chunk_rows):
        chunk = f['packets'][start:start + chunk_rows]
        for iog in np.unique(chunk['io_group']):
            rows = chunk[chunk['io_group'] == iog]
            # Logger message/config metadata may legitimately use IOG 0.
            if not 1 <= iog <= 8:
                if np.any(np.isin(rows['packet_type'], [0, 6, 7])):
                    raise ValueError(f'Data/timing packets have invalid IO group {iog}')
                continue
            if np.any(rows['timestamp'] > np.iinfo(np.int64).max):
                raise ValueError('Packet timestamp exceeds signed 64-bit range')
            out = np.zeros(len(rows), PACKET)
            for target, source in [('kind', 'packet_type'), ('io', 'io_channel'),
                    ('chip', 'chip_id'), ('channel', 'channel_id'),
                    ('timestamp', 'timestamp'), ('receipt', 'receipt_timestamp'),
                    ('subtype', 'trigger_type'), ('parity', 'valid_parity')]:
                out[target] = rows[source]
            if 'fifo_diagnostics_enabled' in rows.dtype.names:
                out['diagnostics'] = rows['fifo_diagnostics_enabled'] != 0
            header = out['kind'] == 4
            idx = np.maximum.accumulate(np.where(header, np.arange(len(rows)) + 1, 0))
            labels = np.r_[last_seconds.get(int(iog), 0), out['timestamp']]
            out['second'] = labels[idx]
            last_seconds[int(iog)] = int(out['second'][-1])
            yield int(iog), out


def batches(path, info, chunk_rows=8192):
    if chunk_rows <= 0:
        raise ValueError('chunk_rows must be positive')
    with h5py.File(path, 'r') as f:
        yield from (_raw_batches(f, chunk_rows) if info['format'] == 'raw'
                    else _packet_batches(f, chunk_rows))
