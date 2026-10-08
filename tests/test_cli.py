import json
import shutil
import subprocess
import numpy as np
import pytest
from raw_movie.cli import main, parser, render_one, validate, expand_inputs
from raw_movie.timeline import Timeline, Replay
from raw_movie.render import Painter
from conftest import write_packets


def test_real_encoder_and_json(tmp_path, geometry, recording_rows):
    source = write_packets(tmp_path / 'packet.h5', recording_rows)
    args = parser().parse_args(['render', str(source), '--width', '640', '--height', '480',
         '--fps', '5', '--duration', '.2', '--tail', '0', '--mode', 'highlight', '--trigger', 'light'])
    validate(args)
    out = tmp_path / 'test.mp4'
    render_one(source, out, geometry, args)
    assert out.stat().st_size > 1000
    report = json.loads(out.with_suffix('.json').read_text())
    assert report['render']['frames'] == 2
    assert report['render']['trigger_window_hits'] == 6
    assert not list(tmp_path.glob('*.lock'))
    assert not list(tmp_path.glob('*.partial*'))
    if shutil.which('ffprobe'):
        result = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0',
            '-show_entries', 'stream=width,height,codec_name,pix_fmt,nb_frames', '-of', 'json', str(out)],
            check=True, capture_output=True, text=True)
        stream = json.loads(result.stdout)['streams'][0]
        assert (stream['width'], stream['height'], stream['nb_frames']) == (640, 480, '2')
        assert (stream['codec_name'], stream['pix_fmt']) == ('h264', 'yuv420p')
    with pytest.raises(FileExistsError):
        render_one(source, out, geometry, args)
    args.skip_existing = True
    before = out.stat().st_mtime_ns
    render_one(source, out, geometry, args)
    assert before == out.stat().st_mtime_ns


def test_encoder_failure_cleans_partial_and_lock(tmp_path, geometry, recording_rows):
    source = write_packets(tmp_path / 'p.h5', recording_rows)
    args = parser().parse_args(['render', str(source), '--ffmpeg', '/bin/false',
                              '--width', '640', '--height', '480', '--duration', '.1'])
    with pytest.raises(RuntimeError, match='FFmpeg'):
        render_one(source, tmp_path / 'failed.mp4', geometry, args)
    assert not list(tmp_path.glob('*.mp4'))
    assert not list(tmp_path.glob('*.partial*'))
    assert not list(tmp_path.glob('*.lock'))


def test_phosphor_fades_without_changing_replay(tmp_path, geometry, recording_rows):
    t = Timeline(write_packets(tmp_path / 'p.h5', recording_rows), geometry, tmp_path / 'runs')
    r = Replay(t)
    try:
        r.advance(.06)
        painter = Painter(t, 1280, 720)
        a = np.asarray(painter.frame(r, .06, .5))
        b = np.asarray(painter.frame(r, 1.06, .5))
        # Compare the detector area only, excluding clock text and progress bar.
        assert a[130:660].sum() > b[130:660].sum()
        assert r.cursor == .06
    finally:
        r.close()


@pytest.mark.parametrize('extra', [ ['--width', '641'], ['--time-mode', 'local', '--trigger', 'beam'],
    ['--mode', 'window'], ['--min-raw-timestamp', '-1'], ['--iogs', '1,2', '--trigger', 'beam'] ])
def test_invalid_options(extra):
    args = parser().parse_args(['render', 'a.h5'] + extra)
    with pytest.raises(ValueError):
        validate(args)


def test_missing_input_is_error(tmp_path):
    with pytest.raises(ValueError, match='did not match'):
        expand_inputs([str(tmp_path / '*.h5')])
    assert main(['inspect', str(tmp_path / 'missing.h5')]) == 1
