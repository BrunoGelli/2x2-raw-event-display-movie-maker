"""One input pass, disk-backed sorted hit runs, and deterministic replay.

Sorting happens AFTER ordered per-IOG unrolling. No hit is dropped for arriving
late. Only latest timestamps per pixel/layer are retained during rendering.
"""
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
import heapq
import logging
import numpy as np
from .reader import batches, inspect_file
from ._upstream.timing import TimingConfig, TimestampUnroller

LOG = logging.getLogger(__name__)
HIT = np.dtype([('tick', '<i8'), ('pixel', '<i4')])


@dataclass
class Source:
    clock: TimestampUnroller = field(default_factory=TimestampUnroller)
    counts: Counter = field(default_factory=Counter)
    candidates: Counter = field(default_factory=Counter)
    sync_labels: set = field(default_factory=set)
    bad_labels: int = 0
    triggers: list = field(default_factory=list)
    pending: list = field(default_factory=list)
    pending_count: int = 0
    runs: list = field(default_factory=list)
    offset: int = 0
    minimum: int | None = None


class Timeline:
    def __init__(self, path, geometry, workdir, *, time_mode='pps',
                 min_raw_timestamp=10, chunk_rows=8192, run_hits=500_000):
        self.info = inspect_file(path)
        self.geometry, self.workdir = geometry, Path(workdir)
        self.sources = {i: Source() for i in geometry.metadata['iogs']}
        self.time_mode = time_mode
        self.period = TimingConfig().rollover_ticks
        self.tick_seconds = TimingConfig().tick_seconds
        self.run_hits = run_hits
        self.workdir.mkdir(parents=True, exist_ok=True)
        seen = 0
        for iog, rows in batches(path, self.info, chunk_rows):
            seen += len(rows)
            if iog not in self.sources:
                continue
            self._consume(iog, rows, min_raw_timestamp)
            if seen >= 5_000_000:
                LOG.info('Read another %.1f million normalized rows', seen / 1e6)
                seen = 0
        for iog in self.sources:
            self._flush(iog)
        self._align()

    def _consume(self, iog, rows, minimum):
        source = self.sources[iog]
        clock = source.clock
        raw_hits = rows['kind'] == 0
        parity_ok = raw_hits & rows['parity']
        source.counts.update(rows=len(rows), charge_packets=int(raw_hits.sum()),
            bad_parity=int((raw_hits & ~rows['parity']).sum()),
            fifo_diagnostics=int((parity_ok & rows['diagnostics']).sum()),
            ignored_syncs=int(((rows['kind'] == 6) & (rows['subtype'] != 83)).sum()),
            other_packets=int((~np.isin(rows['kind'], [0, 4, 6, 7])).sum()))
        data_i = np.flatnonzero(parity_ok & ~rows['diagnostics'])
        data = rows[data_i]
        if np.any(data['timestamp'] >= 2**31):
            raise ValueError('Charge timestamp outside the supported ASIC v2 range')
        sync_i = np.flatnonzero((rows['kind'] == 6) & (rows['subtype'] == 83))
        st = rows['timestamp'][sync_i]
        steps = np.rint(st / self.period).astype(np.int64) * self.period
        good = (steps > 0) & (np.abs(st - steps) <= self.period // 20)
        si = sync_i[good]
        prefix = np.r_[clock.offset, clock.offset + np.cumsum(steps[good], dtype=np.int64)]
        for index, tick in zip(si, prefix[1:]):
            second = int(rows['second'][index])
            if second <= 0 or second in source.sync_labels:
                source.bad_labels += 1
            else:
                source.sync_labels.add(second)
                source.candidates[second * self.period - int(tick)] += 1
        trigger_i = np.flatnonzero(rows['kind'] == 7)
        before = np.searchsorted(si, trigger_i, side='left')
        ready = (clock.initial_tick is not None) | (before > 0)
        traw = rows['timestamp'][trigger_i]
        valid = ready & (traw >= 0) & (traw < self.period)
        source.counts['triggers'] += len(trigger_i)
        source.counts['invalid_trigger_times'] += int((~valid).sum())
        if np.any(valid):
            source.triggers.append((prefix[before] + traw)[valid])

        ticks, valid = clock.consume_arrays(data['timestamp'], data['receipt'],
                                            data_i, sync_i, st)
        address_ok = ((data['io'] >= 1) & (data['io'] <= 32) & (data['channel'] < 64))
        ids = np.full(len(data), -1, dtype=np.int32)
        ids[address_ok] = self.geometry.lut[iog, data['io'][address_ok],
                                            data['chip'][address_ok], data['channel'][address_ok]]
        source.counts['unmapped'] += int((ids < 0).sum())
        source.counts['sync_vetoed'] += int((valid & (ids >= 0) & (data['timestamp'] < minimum)).sum())
        selected = valid & (ids >= 0) & (data['timestamp'] >= minimum)
        source.counts['selected_hits'] += int(selected.sum())
        if np.any(selected):
            out = np.empty(int(selected.sum()), HIT)
            out['tick'], out['pixel'] = ticks[selected], ids[selected]
            source.pending.append(out)
            source.pending_count += len(out)
            lo = int(out['tick'].min())
            source.minimum = lo if source.minimum is None else min(lo, source.minimum)
            if source.pending_count >= self.run_hits:
                self._flush(iog)

    def _flush(self, iog):
        s = self.sources[iog]
        if not s.pending:
            return
        data = np.concatenate(s.pending)
        data.sort(order='tick')
        path = self.workdir / f'iog{iog}-{len(s.runs):06d}.npy'
        np.save(path, data, allow_pickle=False)
        s.runs.append(path)
        s.pending.clear()
        s.pending_count = 0

    def _align(self):
        active = [(i, s) for i, s in self.sources.items()
                  if s.counts['charge_packets'] or s.counts['triggers']]
        if not any(s.counts['selected_hits'] for _, s in active):
            raise ValueError('No usable mapped hits after timing/parity/geometry selection; inspect file and geometry')
        starts, ends = [], []
        for iog, s in active:
            if s.clock.initial_tick is None:
                raise ValueError(f'IOG {iog}: no valid SYNC-83; cannot reconstruct ASIC time')
            if self.time_mode == 'pps':
                if len(s.candidates) != 1 or max(s.candidates.values(), default=0) < 2 or s.bad_labels or s.clock.stats['invalid_syncs']:
                    raise ValueError(f'IOG {iog}: PPS/header epoch is not consistent: '
                        f'{dict(s.candidates)}, ambiguous/missing labels={s.bad_labels}, '
                        f'invalid SYNCs={s.clock.stats["invalid_syncs"]}. '
                        'Use --time-mode local for explicitly UNALIGNED inspection (no detector-wide triggers).')
                s.offset = next(iter(s.candidates))
            else:
                s.offset = -s.clock.initial_tick
            starts.append(min(s.clock.initial_tick, s.minimum if s.minimum is not None else s.clock.initial_tick) + s.offset)
            ends.append(s.clock.frontier_tick + s.offset)
        self.origin, self.end = min(starts), max(ends)
        self.duration = (self.end - self.origin) * self.tick_seconds
        self.active_iogs = [i for i, _ in active]
        self.trigger_ticks = {}
        for iog, s in active:
            if s.triggers:
                self.trigger_ticks[iog] = np.sort(np.concatenate(s.triggers) + (s.offset - self.origin))
        LOG.info('Prepared %.3f detector seconds; %s mapped hits; IOGs %s',
                 self.duration, sum(s.counts['selected_hits'] for s in self.sources.values()), self.active_iogs)

    def report(self):
        return dict(input=self.info, time_mode=self.time_mode,
            timing_method='PPS/header-labelled estimate; hardware phase not calibrated' if self.time_mode == 'pps'
                          else 'UNALIGNED: each IOG starts at its first valid SYNC',
            tick_seconds=self.tick_seconds, origin_tick=int(self.origin), duration_seconds=self.duration,
            geometry=self.geometry.metadata['provenance'], geometry_id=self.geometry.metadata['geometry_id'],
            active_iogs=self.active_iogs,
            sources={str(i): dict(counts=dict(s.counts), timing=s.clock.stats,
                epoch_offset_ticks=int(s.offset), pps_header_candidates={str(k): v for k, v in s.candidates.items()},
                ambiguous_labels=s.bad_labels) for i, s in self.sources.items()},
            temporary_hit_bytes=sum(p.stat().st_size for s in self.sources.values() for p in s.runs))


class Replay:
    """Memory-map sorted runs and apply every due hit, independent of chunk order."""
    def __init__(self, timeline, *, trigger='none', beam_iog=5, light_iog=6, post_us=190):
        if trigger != 'none' and timeline.time_mode != 'pps':
            raise ValueError('Detector-wide highlighting requires --time-mode pps')
        self.timeline = timeline
        self.maps, self.heap = [], []
        for iog, source in timeline.sources.items():
            for path in source.runs:
                data = np.load(path, mmap_mode='r', allow_pickle=False)
                shift = source.offset - timeline.origin
                index = len(self.maps)
                self.maps.append((data, shift))
                heapq.heappush(self.heap, (int(data['tick'][0]) + shift, index, 0))
        self.last = np.full(len(timeline.geometry.pixels), -np.inf)
        self.highlight = self.last.copy()
        self.post = max(1, round(post_us * 1e-6 / timeline.tick_seconds))
        groups = {'none': [], 'beam': [beam_iog], 'light': [light_iog], 'both': [beam_iog, light_iog]}[trigger]
        selected = [timeline.trigger_ticks[i] for i in groups if i in timeline.trigger_ticks]
        self.triggers = np.unique(np.concatenate(selected)) if selected else np.empty(0, dtype=np.int64)
        self.applied = self.matched = 0
        self.cursor = -np.inf

    def advance(self, seconds):
        if seconds < self.cursor:
            raise ValueError('Replay must advance monotonically')
        self.cursor = seconds
        limit = int(round(seconds / self.timeline.tick_seconds))
        while self.heap and self.heap[0][0] <= limit:
            _, index, pos = heapq.heappop(self.heap)
            data, shift = self.maps[index]
            end = int(np.searchsorted(data['tick'], limit - shift, side='right'))
            # Bounded slices even for large skips/speedups.
            for lo in range(pos, end, 100_000):
                rows = data[lo:min(end, lo + 100_000)]
                ticks = rows['tick'] + shift
                values = ticks * self.timeline.tick_seconds
                np.maximum.at(self.last, rows['pixel'], values)
                self.applied += len(rows)
                if len(self.triggers):
                    which = np.searchsorted(self.triggers, ticks, side='right') - 1
                    match = (which >= 0) & (ticks < self.triggers[np.maximum(which, 0)] + self.post)
                    np.maximum.at(self.highlight, rows['pixel'][match], values[match])
                    self.matched += int(match.sum())
            if end < len(data):
                heapq.heappush(self.heap, (int(data['tick'][end]) + shift, index, end))

    def close(self):
        for data, _ in self.maps:
            data._mmap.close()
        self.maps.clear()
