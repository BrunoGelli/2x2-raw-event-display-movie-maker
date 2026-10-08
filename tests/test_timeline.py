import h5py
import numpy as np
import pytest
from raw_movie.reader import inspect_file
from raw_movie.timeline import Timeline, Replay
from conftest import write_packets, write_raw, header, sync, hit, trigger


@pytest.mark.parametrize('chunk', [1, 7, 8192])
def test_raw_packet_parity_and_chunk_invariance(tmp_path, geometry, recording_rows, chunk):
    pkt = write_packets(tmp_path / 'packet.h5', recording_rows)
    raw = write_raw(tmp_path / 'raw.h5', recording_rows)
    a = Timeline(pkt, geometry, tmp_path / 'a', chunk_rows=chunk, run_hits=4)
    b = Timeline(raw, geometry, tmp_path / 'b', chunk_rows=chunk, run_hits=7)
    assert a.origin == b.origin == 1_790_000_000 * 10_000_000
    assert a.duration == b.duration
    ra, rb = Replay(a, trigger='light'), Replay(b, trigger='light')
    try:
        for t in [0, .05, .051, .5, 1, 1.5, 2.99]:
            ra.advance(t)
            rb.advance(t)
            np.testing.assert_array_equal(ra.last, rb.last)
            np.testing.assert_array_equal(ra.highlight, rb.highlight)
        assert ra.matched == rb.matched == 18
        assert ra.applied == rb.applied == 60
        for iog in (1, 5, 6):
            s = a.sources[iog]
            assert s.counts['bad_parity'] == 3
            assert s.counts['sync_vetoed'] == 3
            assert s.counts['unmapped'] == 3
            assert s.clock.stats['boundary_corrected'] == 2
            assert s.counts['ignored_syncs'] == 3
            # Upper trigger boundary is excluded; t0 included.
            assert np.isneginf(ra.highlight[geometry.lut[iog, 1, 11, 2]])
            assert ra.highlight[geometry.lut[iog, 1, 11, 0]] == pytest.approx(2.05)
            # Older arrivals cannot overwrite the latest hit on a pixel.
            assert ra.last[geometry.lut[iog, 1, 11, 4]] == pytest.approx(2.9)
    finally:
        ra.close()
        rb.close()


def test_veto_can_be_disabled(tmp_path, geometry, recording_rows):
    p = write_packets(tmp_path / 'p.h5', recording_rows)
    a = Timeline(p, geometry, tmp_path / 'runs', min_raw_timestamp=0)
    assert all(s.counts['sync_vetoed'] == 0 for s in a.sources.values())
    assert sum(s.counts['selected_hits'] for s in a.sources.values()) == 69


def test_iog_epochs_do_not_depend_on_first_sync_in_file(tmp_path, geometry):
    rows = [header(1, 100), sync(1), hit(1, 100), header(1, 101), sync(1), hit(1, 100)]
    rows += [header(6, 101), sync(6), trigger(6, 100), hit(6, 100),
             header(6, 102), sync(6), hit(6, 100)]
    a = Timeline(write_packets(tmp_path / 'p.h5', rows), geometry, tmp_path / 'runs')
    r = Replay(a, trigger='light')
    try:
        r.advance(a.duration)
        assert r.highlight[geometry.lut[1, 1, 11, 0]] == pytest.approx(1.00001)
        assert r.highlight[geometry.lut[6, 1, 11, 0]] == pytest.approx(1.00001)
        assert r.matched == 2
    finally:
        r.close()


def test_conflicting_epochs_fail_and_local_is_explicit(tmp_path, geometry):
    rows = [header(1, 100), sync(1), hit(1, 100), header(1, 102), sync(1), hit(1, 100)]
    p = write_packets(tmp_path / 'p.h5', rows)
    with pytest.raises(ValueError, match='not consistent'):
        Timeline(p, geometry, tmp_path / 'a')
    local = Timeline(p, geometry, tmp_path / 'b', time_mode='local')
    assert local.origin == 0
    with pytest.raises(ValueError, match='requires'):
        Replay(local, trigger='light')


def test_warmup_invalid_trigger_and_empty_files(tmp_path, geometry):
    rows = [header(1, 100), hit(1, 100), trigger(1, 100), sync(1), hit(1, 200),
            trigger(1, 10_000_000), header(1, 101), sync(1), hit(1, 300)]
    t = Timeline(write_packets(tmp_path / 'p.h5', rows), geometry, tmp_path / 'runs')
    assert t.sources[1].clock.stats['warmup_hits'] == 1
    assert t.sources[1].counts['invalid_trigger_times'] == 2
    assert t.sources[1].counts['selected_hits'] == 2
    with pytest.raises(ValueError, match='No usable'):
        Timeline(write_packets(tmp_path / 'empty.h5', []), geometry, tmp_path / 'empty')


def test_reject_new_format_and_malformed_envelope(tmp_path, geometry, recording_rows):
    p = write_packets(tmp_path / 'p.h5', recording_rows)
    with h5py.File(p, 'a') as f:
        f['_header'].attrs['version'] = '3.0'
    with pytest.raises(ValueError, match='Unsupported packet'):
        inspect_file(p)
    raw = write_raw(tmp_path / 'raw.h5', recording_rows)
    with h5py.File(raw, 'a') as f:
        f['msgs'][0] = f['msgs'][0][:-1]
    with pytest.raises(ValueError, match='length mismatch'):
        Timeline(raw, geometry, tmp_path / 'r')


def test_same_pixel_is_mapped_through_each_hydra_channel(geometry):
    assert len(set(int(geometry.lut[1, io, 11, 0]) for io in range(1, 5))) == 1
