"""Streaming ASIC-time unrolling and bounded, detector-time phosphor playback.

Rollover arithmetic follows ndlar_flow RawEventBuilder.unroll_timestamps() at
commit a0eb2f364e35340d67fd73dc09a8e8f847211a58. Unlike the file-local helper,
this object retains the cumulative offset and previous SYNC across batches.
Each instance is ONE IO group. These relative epochs are NOT an absolute
cross-IOG/trigger alignment calibration.
"""
from dataclasses import dataclass
import heapq
import numpy as np
from .post_sync import display_mask, validate_min_raw_timestamp


@dataclass(frozen=True)
class TimingConfig:
    tick_seconds: float = 1e-7
    rollover_ticks: int = 10_000_000
    sync_type: int = 83             # ASCII S, NOT heartbeat H (72)
    playback_delay: float = 1.25
    max_pending_hits: int = 500_000 # per IOG, bounded even if UI/clock stalls
    max_pending_chunks: int = 8192
    min_raw_timestamp: int = 10   # raw ASIC ticks 0..9 are display-vetoed; 0 disables

    def __post_init__(self):
        validate_min_raw_timestamp(self.min_raw_timestamp)
        if not np.isfinite(self.tick_seconds) or self.tick_seconds <= 0:
            raise ValueError('tick_seconds must be finite and positive')
        if not 1 <= self.rollover_ticks <= 2**31:
            raise ValueError('rollover_ticks must be in 1..2**31')
        if not 0 <= self.sync_type <= 255:
            raise ValueError('sync_type must be a byte')
        if not np.isfinite(self.playback_delay) or not 0.05 <= self.playback_delay <= 10:
            raise ValueError('playback_delay must be 0.05..10 seconds')
        if not 1 <= self.max_pending_hits <= 5_000_000 or not 1 <= self.max_pending_chunks <= 65536:
            raise ValueError('pending-buffer limits are out of range')


class TimestampUnroller:
    def __init__(self, config=None):
        self.config = config or TimingConfig()
        self.offset = 0
        self.last_step = self.config.rollover_ticks
        self.initial_tick = None
        self.frontier_tick = None
        self.last_hit_tick = -1
        self.stats = dict(pps_syncs=0, ignored_syncs=0, invalid_syncs=0,
                          missed_periods=0, boundary_corrected=0, warmup_hits=0,
                          invalid_times=0, out_of_order_hits=0)

    def consume(self, hits):
        """Return int64 hit ticks and a timing-valid mask in accepted-hit order.

        Called also on SYNC-only batches. The wire buffer is read, never sorted
        or rewritten before determining which SYNC precedes each hit.
        """
        words = hits.words
        if words is None:
            return np.empty(0, dtype=np.int64), np.empty(0, dtype=bool)
        sync_indices = np.flatnonzero(words['kind'] == ord('S'))
        selected = words['io'][sync_indices] == self.config.sync_type
        self.stats['ignored_syncs'] += int(np.count_nonzero(~selected))
        sync_indices = sync_indices[selected]
        # SYNC timestamp occupies word bytes 4..7, not DATA receipt bytes 2..5.
        aux = np.ndarray((len(words),), dtype='<u4', buffer=words, offset=4,
                         strides=(words.dtype.itemsize,)) if len(words) else np.empty(0, dtype='<u4')
        return self.consume_arrays(hits.timestamp, hits.receipt_timestamp,
                                   hits.word_indices, sync_indices, aux[sync_indices])

    def consume_arrays(self, timestamp, receipt, hit_indices, sync_indices, sync_timestamp):
        """Array API for file validation; indices must share the same wire order."""
        raw = np.asarray(timestamp, dtype=np.int64)
        receipt = np.asarray(receipt, dtype=np.int64)
        hi = np.asarray(hit_indices, dtype=np.int64)
        si = np.asarray(sync_indices, dtype=np.int64)
        st = np.asarray(sync_timestamp, dtype=np.int64)
        if not (len(raw) == len(receipt) == len(hi)) or len(si) != len(st):
            raise ValueError('inconsistent timing array lengths')
        period = self.config.rollover_ticks
        steps = (np.rint(st / period).astype(np.int64) * period)
        # Zero/implausibly phased SYNC is not silently a valid PPS. Fail visible.
        good_sync = (steps > 0) & (np.abs(st - steps) <= max(1, period // 20))
        self.stats['invalid_syncs'] += int(np.count_nonzero(~good_sync))
        si, steps = si[good_sync], steps[good_sync]
        if len(si) > 1 and np.any(np.diff(si) <= 0):
            raise ValueError('SYNC indices must be strictly ordered')
        prefix = np.r_[self.offset, self.offset + np.cumsum(steps, dtype=np.int64)]
        previous_steps = np.r_[self.last_step, steps]
        before = np.searchsorted(si, hi, side='left')
        ready = np.full(len(raw), self.initial_tick is not None, dtype=bool) | (before > 0)
        offsets = prefix[before]
        crossed = receipt < raw
        ticks = raw % period + offsets - crossed * previous_steps[before]
        receipt_ticks = offsets + receipt
        # A corrupted receipt word must not propel playback hundreds of seconds.
        valid = ready & (ticks >= 0) & (receipt_ticks >= ticks) & (receipt < 32 * period)
        self.stats['warmup_hits'] += int(np.count_nonzero(~ready))
        self.stats['invalid_times'] += int(np.count_nonzero(ready & ~valid))
        self.stats['boundary_corrected'] += int(np.count_nonzero(valid & crossed))
        tv = ticks[valid]
        if len(tv):
            prior_max = np.maximum.accumulate(np.r_[self.last_hit_tick, tv])[:-1]
            self.stats['out_of_order_hits'] += int(np.count_nonzero(tv < prior_max))
            self.last_hit_tick = max(self.last_hit_tick, int(tv.max()))
            frontier = int(receipt_ticks[valid].max())
            self.frontier_tick = max(self.frontier_tick or 0, frontier)
        if len(steps):
            if self.initial_tick is None:
                self.initial_tick = int(prefix[1])
            self.offset = int(prefix[-1])
            self.last_step = int(steps[-1])
            self.frontier_tick = max(self.frontier_tick or 0, self.offset)
            self.stats['pps_syncs'] += len(steps)
            self.stats['missed_periods'] += int(np.sum(steps // period - 1))
        return ticks, valid
