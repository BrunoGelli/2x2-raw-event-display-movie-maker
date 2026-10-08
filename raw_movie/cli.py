"""Batch CLI: one finalized MP4 and a provenance/counters JSON per input file."""
import argparse
import glob
import json
import logging
import math
from pathlib import Path
import tempfile
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from . import __version__
from ._upstream.geometry import load_geometry, download_geometry, demo_geometry
from .reader import inspect_file
from .timeline import Timeline
from .render import render_movie, ffmpeg_executable

LOG = logging.getLogger(__name__)


def positive(value):
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise argparse.ArgumentTypeError('must be finite and positive')
    return result


def nonnegative(value):
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise argparse.ArgumentTypeError('must be finite and nonnegative')
    return result


def positive_int(value):
    result = int(value)
    if result <= 0:
        raise argparse.ArgumentTypeError('must be a positive integer')
    return result


def iog_list(value):
    try:
        groups = sorted(set(map(int, value.split(','))))
        if not groups or any(i not in range(1, 9) for i in groups):
            raise ValueError()
        return groups
    except ValueError as exc:
        raise argparse.ArgumentTypeError('use comma-separated IO groups in 1..8') from exc


def add_options(p):
    p.add_argument('--geometry-dir', type=Path, default=Path('layout'))
    p.add_argument('--iogs', type=iog_list, default=list(range(1, 9)))
    p.add_argument('--time-mode', choices=['pps', 'local'], default='pps')
    p.add_argument('--min-raw-timestamp', type=int, default=10, help='display veto below this raw ASIC tick; 0 disables')
    p.add_argument('--chunk-rows', type=positive_int, default=8192, help='HDF5 rows per read (messages for raw input)')
    p.add_argument('--run-hits', type=positive_int, default=500_000, help='target hits per sorted disk run, per IOG')
    p.add_argument('--work-dir', type=Path, help='parent for temporary hit runs; defaults to TMPDIR')
    p.add_argument('--width', type=positive_int, default=1920)
    p.add_argument('--height', type=positive_int, default=1080)
    p.add_argument('--fps', type=positive_int, default=30)
    p.add_argument('--speed', type=positive, default=1, help='detector seconds per movie second')
    p.add_argument('--decay', type=positive, default=.35, help='phosphor exponential time constant, in movie seconds')
    p.add_argument('--start', type=nonnegative, default=0, help='detector seconds after the file timeline origin')
    p.add_argument('--duration', type=positive, help='detector seconds to show (entire input still scanned for timing)')
    p.add_argument('--tail', type=nonnegative, default=1, help='extra fading movie seconds after data end')
    p.add_argument('--mode', choices=['all', 'highlight', 'window'], default='all')
    p.add_argument('--trigger', choices=['none', 'beam', 'light', 'both'], default='none')
    p.add_argument('--beam-iog', type=int, choices=range(1, 9), default=5)
    p.add_argument('--light-iog', type=int, choices=range(1, 9), default=6)
    p.add_argument('--post-us', type=positive, default=190, help='half-open trigger window [t0,t0+post), microseconds')
    p.add_argument('--timezone', default='America/Chicago')
    p.add_argument('--ffmpeg', help='FFmpeg executable; otherwise system/bundled binary')
    p.add_argument('--crf', type=int, choices=range(0, 52), default=18)
    p.add_argument('--threads', type=positive_int, default=4, help='FFmpeg encoding threads')
    p.add_argument('--overwrite', action='store_true')
    p.add_argument('--skip-existing', action='store_true', help='skip when both MP4 and JSON are already present')


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--version', action='version', version=__version__)
    sub = p.add_subparsers(dest='command', required=True)
    fetch = sub.add_parser('fetch-geometry', help='explicit one-time download of pinned PacMon geometry')
    fetch.add_argument('--geometry-dir', type=Path, default=Path('layout'))
    check = sub.add_parser('inspect', help='show HDF5 schema/metadata without loading geometry or rendering')
    check.add_argument('inputs', nargs='+')
    render = sub.add_parser('render', help='render one video for each input HDF5 file')
    render.add_argument('inputs', nargs='+', help='files, directories (*.h5), or quoted globs')
    render.add_argument('--recursive', action='store_true', help='recurse into input directories')
    render.add_argument('--output-dir', type=Path, default=Path('movies'))
    render.add_argument('--continue-on-error', action='store_true', help='finish other files, then exit nonzero if any failed')
    add_options(render)
    demo = sub.add_parser('demo', help='make an explicitly SYNTHETIC smoke-test movie')
    demo.add_argument('--output', type=Path, default=Path('demo.mp4'))
    add_options(demo)
    return p


def expand_inputs(values, recursive=False):
    paths = []
    for value in values:
        matches = sorted(glob.glob(value, recursive=recursive))
        if not matches:
            raise ValueError(f'Input did not match any files: {value}')
        for match in matches:
            path = Path(match)
            files = sorted(path.rglob('*.h5') if recursive else path.glob('*.h5')) if path.is_dir() else [path]
            if not files:
                raise ValueError(f'No HDF5 files in {path}')
            paths.extend(p.resolve() for p in files)
    return list(dict.fromkeys(paths))


def validate(args):
    if args.width < 640 or args.height < 480 or args.width % 2 or args.height % 2:
        raise ValueError('Use even dimensions of at least 640x480 for H.264')
    if not 1.2 <= args.width / args.height <= 2.4:
        raise ValueError('Use a landscape aspect ratio between 1.2 and 2.4')
    if not 0 <= args.min_raw_timestamp <= 2**31:
        raise ValueError('--min-raw-timestamp must be in 0..2**31')
    if args.mode != 'all' and args.trigger == 'none':
        raise ValueError('--mode highlight/window requires --trigger beam/light/both')
    if args.time_mode == 'local' and args.trigger != 'none':
        raise ValueError('--time-mode local requires --trigger none')
    if args.overwrite and args.skip_existing:
        raise ValueError('Choose either --overwrite or --skip-existing')
    if args.beam_iog == args.light_iog:
        raise ValueError('Beam and Light IO groups must differ')
    required_sources = {'none': [], 'beam': [args.beam_iog], 'light': [args.light_iog],
                        'both': [args.beam_iog, args.light_iog]}[args.trigger]
    if any(i not in args.iogs for i in required_sources):
        raise ValueError('--iogs must include the selected trigger source(s)')
    try:
        ZoneInfo(args.timezone)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f'Unknown timezone {args.timezone}; install tzdata or use UTC') from exc
    ffmpeg_executable(args.ffmpeg)
    if args.work_dir:
        args.work_dir.mkdir(parents=True, exist_ok=True)


def render_one(source, output, geometry, args, *, title=None):
    output = output.resolve()
    report_path = output.with_suffix('.json')
    if source.resolve() in (output, report_path):
        raise ValueError('Output must not overwrite the input')
    if args.skip_existing and output.is_file() and output.stat().st_size and report_path.is_file():
        LOG.info('Skipping completed output %s', output)
        return
    if not args.overwrite and (output.exists() or report_path.exists()):
        raise FileExistsError(f'Output exists: {output} or {report_path}; use --overwrite or --skip-existing')
    output.parent.mkdir(parents=True, exist_ok=True)
    lock = output.with_suffix('.mp4.lock')
    try:
        lock_file = lock.open('x')
    except FileExistsError as exc:
        raise ValueError(f'Output locked: {lock}. Check for another job; remove only a stale lock.') from exc
    temp_output = temp_report = None
    try:
        with lock_file:
            import os
            lock_file.write(f'pid={os.getpid()}\n')
        # Recheck under the lock; another job may have completed since preflight.
        if not args.overwrite and (output.exists() or report_path.exists()):
            raise FileExistsError(f'Output appeared during startup: {output}')
        stat_before = source.stat()
        with tempfile.TemporaryDirectory(prefix='raw-movie-', dir=args.work_dir) as work:
            timeline = Timeline(source, geometry, work, time_mode=args.time_mode,
                min_raw_timestamp=args.min_raw_timestamp, chunk_rows=args.chunk_rows, run_hits=args.run_hits)
            stat_after = source.stat()
            if (stat_before.st_size, stat_before.st_mtime_ns) != (stat_after.st_size, stat_after.st_mtime_ns):
                raise ValueError('Input changed during reading; render closed, fully transferred files only')
            with tempfile.NamedTemporaryFile(prefix=output.stem + '.', suffix='.partial.mp4', dir=output.parent, delete=False) as tmp:
                temp_output = Path(tmp.name)
            options = dict(width=args.width, height=args.height, fps=args.fps, speed=args.speed,
                decay=args.decay, start=args.start, duration=args.duration, tail=args.tail,
                mode=args.mode, trigger=args.trigger, beam_iog=args.beam_iog, light_iog=args.light_iog,
                post_us=args.post_us, timezone_name=args.timezone, ffmpeg=args.ffmpeg, crf=args.crf, threads=args.threads)
            result = render_movie(timeline, temp_output, title=title, **options)
            report = timeline.report()
            report.update(version=__version__, render=result, options=options,
                          min_raw_timestamp=args.min_raw_timestamp, output=str(output),
                          source_size=stat_before.st_size, source_mtime_ns=stat_before.st_mtime_ns,
                          synthetic=title is not None)
            with tempfile.NamedTemporaryFile(mode='w', prefix=output.stem + '.', suffix='.partial.json', dir=output.parent, delete=False) as tmp:
                temp_report = Path(tmp.name)
                json.dump(report, tmp, indent=2)
                tmp.write('\n')
            temp_output.replace(output)
            temp_report.replace(report_path)
        LOG.info('Saved %s and %s', output, report_path)
    finally:
        if temp_output:
            temp_output.unlink(missing_ok=True)
        if temp_report:
            temp_report.unlink(missing_ok=True)
        lock.unlink(missing_ok=True)


def main(argv=None):
    args = parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    try:
        if args.command == 'fetch-geometry':
            download_geometry(args.geometry_dir)
            return 0
        if args.command == 'inspect':
            print(json.dumps([inspect_file(p) for p in expand_inputs(args.inputs)], indent=2))
            return 0
        validate(args)
        if args.command == 'demo':
            from .demo import make_demo
            geometry = demo_geometry(args.iogs)
            with tempfile.TemporaryDirectory(prefix='raw-movie-demo-', dir=args.work_dir) as work:
                source = make_demo(Path(work) / 'SYNTHETIC-packets.h5', geometry)
                render_one(source, args.output, geometry, args, title='SYNTHETIC DEMO  /  NOT DETECTOR DATA')
            return 0
        inputs = expand_inputs(args.inputs, args.recursive)
        targets = [args.output_dir / (p.stem + '.mp4') for p in inputs]
        if len(set(targets)) != len(targets):
            raise ValueError('Inputs have duplicate basenames; use separate output directories')
        geometry = load_geometry(args.geometry_dir, args.iogs)
        failures = 0
        for source, output in zip(inputs, targets):
            try:
                LOG.info('Processing %s', source)
                render_one(source, output, geometry, args)
            except (ValueError, OSError, RuntimeError) as exc:
                failures += 1
                LOG.error('%s: %s', source, exc)
                if not args.continue_on_error:
                    break
        return int(failures > 0)
    except (ValueError, OSError, RuntimeError) as exc:
        LOG.error('%s', exc)
        return 1
    except KeyboardInterrupt:
        LOG.error('Interrupted; unfinished output removed')
        return 130
