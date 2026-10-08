"""Batched legacy PACMAN/Packet_v2 decoding, retaining ordered timing words.

No change to wire framing, parity policy or downstream acceptance. SYNC words
must remain in wire order until timestamp unrolling has consumed them.
"""
from dataclasses import dataclass
import struct
from typing import Optional
import numpy as np

HEADER = struct.Struct('<cIxH')
WORD = np.dtype({'names': ['kind', 'io', 'receipt', 'payload'],
                 'formats': ['u1', 'u1', '<u4', '<u8'],
                 'offsets': [0, 1, 2, 8], 'itemsize': 16})
MAX_MESSAGE = HEADER.size + 65535 * WORD.itemsize
DECODE_COUNTERS = (
    'words', 'data_words', 'data_hits', 'valid_data_hits', 'bad_parity',
    'downstream', 'upstream', 'other_packets', 'packet_type_0', 'packet_type_1',
    'packet_type_2', 'packet_type_3', 'triggers', 'sync', 'unknown_words',
    'nondata_messages', 'legacy_messages', 'malformed', 'sync83_packets',
)

class DecodeError(ValueError):
    """Unsupported or malformed legacy PACMAN envelope."""

@dataclass
class Hits:
    io_channel: np.ndarray
    chip: np.ndarray
    channel: np.ndarray
    adc: np.ndarray
    timestamp: np.ndarray
    receipt_timestamp: np.ndarray
    counters: dict
    diagnostic: Optional[str] = None
    # Optional references: no copy of the raw words; indices follow accepted hits.
    words: Optional[np.ndarray] = None
    word_indices: Optional[np.ndarray] = None
    # One entry per nonempty DATA envelope, not per charge hit.
    message_ends: Optional[np.ndarray] = None
    message_seconds: Optional[np.ndarray] = None


def odd_parity(payload):
    p = payload.copy()
    for shift in (32, 16, 8, 4, 2, 1):
        p ^= p >> np.uint64(shift)
    return (p & np.uint64(1)) == 1


def _empty(counters, diagnostic=None, words=None, ends=None, seconds=None):
    u8 = np.empty(0, dtype=np.uint8)
    u32 = np.empty(0, dtype=np.uint32)
    return Hits(u8, u8, u8, u8, u32, u32, counters, diagnostic,
                words, np.empty(0, dtype=np.int64), ends, seconds)


def _legacy_header(message):
    if not isinstance(message, (bytes, bytearray, memoryview)):
        raise DecodeError('message must be bytes-like')
    if len(message) < HEADER.size or len(message) > MAX_MESSAGE:
        raise DecodeError(f'invalid legacy PACMAN message length {len(message)}')
    kind, _, nwords = HEADER.unpack_from(message)
    if kind not in (b'D', b'?', b'!'):
        raise DecodeError(f'unsupported legacy PACMAN message type {kind!r}')
    expected = HEADER.size + nwords * WORD.itemsize
    if len(message) != expected:
        raise DecodeError(f'legacy PACMAN length mismatch: header says {nwords} words '
                          f'({expected} bytes total), got {len(message)}')
    return kind, nwords


def decode_batch(messages, *, strict=False):
    counters = {name: 0 for name in DECODE_COUNTERS}
    bodies, first_error = [], None
    ends, seconds = [], []
    for message in messages:
        try:
            kind, nwords = _legacy_header(message)
        except DecodeError as exc:
            if strict:
                raise
            counters['malformed'] += 1
            if first_error is None:
                raw = bytes(message) if isinstance(message, (bytes, bytearray, memoryview)) else b''
                first_error = f'{exc}; len={len(raw)} prefix32={raw[:32].hex()}'
            continue
        counters['legacy_messages'] += 1
        if kind != b'D':
            counters['nondata_messages'] += 1
            continue
        counters['words'] += nwords
        if nwords:
            bodies.append(memoryview(message)[HEADER.size:])
            ends.append(counters['words'])
            seconds.append(HEADER.unpack_from(message)[1])
    if not bodies:
        return _empty(counters, first_error)
    body = bodies[0] if len(bodies) == 1 else b''.join(bodies)
    words = np.frombuffer(body, dtype=WORD)
    ends = np.asarray(ends, dtype=np.int64)
    seconds = np.asarray(seconds, dtype=np.int64)
    kinds = words['kind']
    counters['triggers'] = int(np.count_nonzero(kinds == ord('T')))
    counters['sync'] = int(np.count_nonzero(kinds == ord('S')))
    counters['sync83_packets'] = int(np.count_nonzero((kinds == ord('S')) & (words['io'] == 83)))
    counters['unknown_words'] = int(np.count_nonzero(~np.isin(kinds, [ord(x) for x in 'DTSPWRE'])))
    indices = np.flatnonzero(kinds == ord('D'))
    data = words[indices]
    counters['data_words'] = len(data)
    if not len(data):
        return _empty(counters, first_error, words, ends, seconds)
    ptype = (data['payload'] & np.uint64(3)).astype(np.uint8)
    for value in range(4):
        counters[f'packet_type_{value}'] = int(np.count_nonzero(ptype == value))
    mask = ptype == 0
    counters['data_hits'] = int(np.count_nonzero(mask))
    counters['other_packets'] = len(mask) - counters['data_hits']
    data, indices = data[mask], indices[mask]
    if not len(data):
        return _empty(counters, first_error, words, ends, seconds)
    parity = odd_parity(data['payload'])
    downstream = ((data['payload'] >> np.uint64(62)) & np.uint64(1)) != 0
    counters['valid_data_hits'] = int(np.count_nonzero(parity))
    counters['bad_parity'] = len(parity) - counters['valid_data_hits']
    counters['downstream'] = int(np.count_nonzero(downstream))
    counters['upstream'] = len(downstream) - counters['downstream']
    data, indices = data[parity], indices[parity]
    p = data['payload']
    return Hits(data['io'], ((p >> np.uint64(2)) & 255).astype(np.uint8),
                ((p >> np.uint64(10)) & 63).astype(np.uint8),
                ((p >> np.uint64(48)) & 255).astype(np.uint8),
                ((p >> np.uint64(16)) & 0x7fffffff).astype(np.uint32),
                data['receipt'], counters, first_error, words, indices, ends, seconds)


def decode(message):
    return decode_batch([message], strict=True)


def make_message(io, chip, channel, adc=80, timestamp=1234, *, downstream=False):
    io, chip, channel, adc, timestamp, downstream = np.broadcast_arrays(
        io, chip, channel, adc, timestamp, downstream)
    if io.size > 65535:
        raise ValueError('one synthetic legacy message can hold at most 65535 words')
    if np.any((io < 1) | (io > 255) | (chip < 0) | (chip > 255) |
              (channel < 0) | (channel > 63) | (adc < 0) | (adc > 255)):
        raise ValueError('synthetic field out of range')
    p = ((chip.astype(np.uint64).ravel() << np.uint64(2)) |
         (channel.astype(np.uint64).ravel() << np.uint64(10)) |
         ((timestamp.astype(np.uint64).ravel() & np.uint64(0x7fffffff)) << np.uint64(16)) |
         (adc.astype(np.uint64).ravel() << np.uint64(48)) |
         (downstream.astype(np.uint64).ravel() << np.uint64(62)))
    p |= (~odd_parity(p)).astype(np.uint64) << np.uint64(63)
    words = np.zeros(io.size, dtype=WORD)
    words['kind'], words['io'], words['receipt'], words['payload'] = ord('D'), io.ravel(), 4321, p
    return HEADER.pack(b'D', 0, io.size) + words.tobytes()
