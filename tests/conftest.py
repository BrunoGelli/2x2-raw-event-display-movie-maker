import struct
import h5py
import numpy as np
import pytest
from raw_movie._upstream.geometry import demo_geometry
from raw_movie.demo import DTYPE


@pytest.fixture
def geometry():
    return demo_geometry([1, 5, 6], chips_per_tile=1)


def write_packets(path, rows):
    with h5py.File(path, 'w') as f:
        f.create_group('_header').attrs['version'] = '2.4'
        f.create_dataset('packets', data=np.array(rows, DTYPE))
    return path


def write_raw(path, rows):
    """Independent wire fixture using the documented bit/byte positions."""
    messages, iogs = [], []
    second, body, current_iog = None, [], None
    def flush():
        if second is not None:
            messages.append(struct.pack('<cIxH', b'D', second, len(body)) + b''.join(body))
            iogs.append(current_iog)
    for row in np.array(rows, DTYPE):
        kind = int(row['packet_type'])
        if kind == 4:
            flush()
            second, current_iog, body = int(row['timestamp']), int(row['io_group']), []
        elif kind == 0:
            payload = (int(row['chip_id']) << 2 | int(row['channel_id']) << 10 |
                       int(row['timestamp']) << 16 | int(row['dataword']) << 48)
            payload |= ((payload.bit_count() + 1) % 2) << 63
            if not row['valid_parity']:
                payload ^= 1 << 63
            body.append(struct.pack('<BBI2xQ', ord('D'), int(row['io_channel']), int(row['receipt_timestamp']), payload))
        elif kind in (6, 7):
            body.append(struct.pack('<BB2xI8x', ord('S') if kind == 6 else ord('T'),
                                    int(row['trigger_type']), int(row['timestamp'])))
        else:
            raise ValueError(kind)
    flush()
    with h5py.File(path, 'w') as f:
        meta = f.create_group('meta')
        meta.attrs['version'], meta.attrs['io_version'] = '0.0', '0.0'
        msgs = f.create_dataset('msgs', (len(messages),), dtype=h5py.vlen_dtype(np.dtype('u1')))
        for i, msg in enumerate(messages):
            msgs[i] = np.frombuffer(msg, dtype='u1')
        f.create_dataset('msg_headers', data=np.array([(i,) for i in iogs], dtype=[('io_groups', 'u1')]))
    return path


def header(iog, second):
    return (iog, 0, 0, 4, 0, 0, second, 0, 0, 0)


def sync(iog, tick=10_000_000, subtype=83):
    return (iog, 0, 0, 6, 0, 0, tick, 0, subtype, 0)


def hit(iog, tick, *, receipt=None, channel=0, parity=1, io=1):
    return (iog, io, 11, 0, parity, channel, tick, 80, 0, tick + 10 if receipt is None else receipt)


def trigger(iog, tick):
    return (iog, 0, 0, 7, 0, 0, tick, 0, 2, 0)


@pytest.fixture
def recording_rows():
    rows = []
    for second in range(3):
        for iog in (1, 5, 6):
            rows.extend([header(iog, 1_790_000_000 + second), sync(iog),
                         hit(iog, 5), hit(iog, 10), hit(iog, 10_000, parity=0),
                         sync(iog, 123, 72), hit(iog, 500_000), hit(iog, 501_899, channel=1),
                         hit(iog, 501_900, channel=2), hit(iog, 600_000, io=0)])
            if iog == 6:
                # Arrives AFTER charge: offline replay must still highlight it.
                rows.append(trigger(iog, 500_000))
            if second:
                rows.append(hit(iog, 9_999_990, receipt=20, channel=3))
            rows.extend([hit(iog, 9_000_000, channel=4), hit(iog, 8_000_000, channel=4)])
    return rows
