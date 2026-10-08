"""Headless Pillow rasterizer and streaming H.264 encoder; no browser/GPU."""
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import logging
import math
import os
import shutil
import subprocess
import tempfile
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from .timeline import Replay

LOG = logging.getLogger(__name__)
BG = (7, 18, 26)
TILE_BG = np.array([4, 13, 20])


def font(size):
    try:
        return ImageFont.truetype('DejaVuSans.ttf', size)
    except OSError:
        return ImageFont.load_default(size=size)


class Painter:
    def __init__(self, timeline, width, height, *, speed=1, decay=.35,
                 mode='all', trigger='none', timezone_name='America/Chicago', title=None):
        self.timeline, self.width, self.height = timeline, width, height
        self.speed, self.decay, self.mode = speed, decay, mode
        self.tz = ZoneInfo(timezone_name)
        self.scale = width / 1920
        self.big, self.medium, self.small = [font(max(10, round(s * self.scale))) for s in (30, 21, 17)]
        self.base = Image.new('RGB', (width, height), BG)
        d = ImageDraw.Draw(self.base)
        margin = round(24 * self.scale)
        self.margin = margin
        d.text((margin, margin), title or '2x2  /  RAW DETECTOR REPLAY', font=self.big, fill='#ecf5f9')
        name = Path(timeline.info['path']).name
        while d.textlength(name, font=self.small) > width - 2 * margin:
            name = name[:-5] + '...'
        d.text((margin, round(66 * self.scale)), name, font=self.small, fill='#8ba6b6')
        self.tiles = []
        modules = sorted({(i - 1) // 2 for i in timeline.geometry.metadata['iogs']})
        cols = min(2, len(modules))
        rows = math.ceil(len(modules) / cols)
        y_start, y_end = round(142 * self.scale), height - round(100 * self.scale)
        mw = (width - margin * (cols + 1)) / cols
        mh = (y_end - y_start - margin * (rows - 1)) / rows
        self.badges = []
        for number, module in enumerate(modules):
            mx = margin + (number % cols) * (mw + margin)
            my = y_start + (number // cols) * (mh + margin)
            d.rounded_rectangle((mx, my, mx + mw, my + mh), radius=8, fill='#0d202c', outline='#234353')
            d.text((mx + 12, my + 8 * self.scale), f'MODULE {module}', font=self.medium, fill='#d7e6ed')
            groups = [i for i in timeline.geometry.metadata['iogs'] if (i - 1) // 2 == module]
            for plane, iog in enumerate(groups):
                px = mx + plane * mw / len(groups)
                pw = mw / len(groups)
                label = f'IOG {iog}' + ('  /  no data' if iog not in timeline.active_iogs else '')
                d.text((px + 12, my + 40 * self.scale), label, font=self.small, fill='#8caab9')
                tiles = [t for t in timeline.geometry.metadata['tiles'] if t['iog'] == iog]
                xmin = min(t['x_min'] - t['pitch'] / 2 for t in tiles)
                xmax = max(t['x_min'] + (t['width'] - .5) * t['pitch'] for t in tiles)
                ymin = min(t['y_min'] - t['pitch'] / 2 for t in tiles)
                ymax = max(t['y_min'] + (t['height'] - .5) * t['pitch'] for t in tiles)
                top, bottom = my + 70 * self.scale, my + mh - 14 * self.scale
                factor = min((pw - 30 * self.scale) / (xmax - xmin), (bottom - top) / (ymax - ymin))
                ox = px + pw / 2 - factor * (xmin + xmax) / 2
                oy = (top + bottom) / 2 + factor * (ymin + ymax) / 2
                for t in tiles:
                    x = round(ox + factor * (t['x_min'] - t['pitch'] / 2))
                    y = round(oy - factor * (t['y_min'] + (t['height'] - .5) * t['pitch']))
                    w, h = max(1, round(t['width'] * t['pitch'] * factor)), max(1, round(t['height'] * t['pitch'] * factor))
                    rec = timeline.geometry.pixels[t['start']:t['start'] + t['count']]
                    col, row = rec['col'], t['height'] - 1 - rec['row']
                    pooled = None
                    if w < t['width'] or h < t['height']:
                        pooled = np.minimum(h - 1, ((row + .5) * h / t['height']).astype(int)) * w
                        pooled += np.minimum(w - 1, ((col + .5) * w / t['width']).astype(int))
                    self.tiles.append((t, x, y, w, h, col, row, pooled))
                    d.rectangle((x - 1, y - 1, x + w, y + h), outline='#355969')
        note = f'{speed:g}x playback  |  fade {decay:g}s of movie time  |  {mode} / {trigger}'
        d.text((margin, height - round(78 * self.scale)), note, font=self.small, fill='#aac0cd')
        note = ('PPS/header time estimate  |  green: charge activity  |  yellow: trigger-window candidates'
                if timeline.time_mode == 'pps' else 'UNALIGNED LOCAL CLOCKS  |  no detector-wide coincidence claim')
        d.text((margin, height - round(49 * self.scale)), note, font=self.small,
               fill='#8ba6b6' if timeline.time_mode == 'pps' else '#ffc861')

    def frame(self, replay, seconds, progress):
        frame = self.base.copy()
        d = ImageDraw.Draw(frame)
        if self.timeline.time_mode == 'pps':
            origin_second, origin_rest = divmod(self.timeline.origin, self.timeline.period)
            stamp = datetime.fromtimestamp(origin_second + origin_rest / self.timeline.period + seconds, timezone.utc).astimezone(self.tz)
            clock = stamp.strftime('%Y-%m-%d  %H:%M:%S.') + f'{stamp.microsecond // 1000:03d} ' + stamp.strftime('%Z')
        else:
            clock = f'UNALIGNED  +{seconds:.3f} s'
        d.text((self.margin, round(104 * self.scale)), clock, font=self.medium, fill='#7ae8bd')
        count_text = f'{replay.applied:,} hits replayed'
        d.text((self.width - self.margin - d.textlength(count_text, font=self.small), round(108 * self.scale)), count_text, font=self.small, fill='#8ba6b6')
        # Fade in movie seconds, including at accelerated playback rates.
        brightness = np.exp(np.minimum(0., (replay.last - seconds) / (self.decay * self.speed)))
        yellow = np.exp(np.minimum(0., (replay.highlight - seconds) / (self.decay * self.speed)))
        if self.mode == 'window':
            brightness.fill(0)
        colors = TILE_BG + brightness[:, None] * (np.array([94, 244, 181]) - TILE_BG)
        if self.mode != 'all':
            strength = np.maximum(brightness, yellow)
            use = (yellow > 1 / 255) & (yellow >= brightness * .75)
            colors[use] = TILE_BG + strength[use, None] * (np.array([255, 208, 76]) - TILE_BG)
        colors = np.clip(colors, 0, 255).astype(np.uint8)
        for t, x, y, w, h, col, row, pooled in self.tiles:
            tile_colors = colors[t['start']:t['start'] + t['count']]
            if pooled is not None:
                raster = np.empty((h, w, 3), dtype=np.uint8)
                raster[:] = TILE_BG
                # Preserve isolated active pixels when several detector pixels
                # share a movie pixel. Nearest/average downsampling loses them.
                np.maximum.at(raster.reshape(-1, 3), pooled, tile_colors)
                tile_image = Image.fromarray(raster)
            else:
                raster = np.empty((t['height'], t['width'], 3), dtype=np.uint8)
                raster[:] = TILE_BG
                raster[row, col] = tile_colors
                tile_image = Image.fromarray(raster).resize((w, h), Image.Resampling.NEAREST)
            frame.paste(tile_image, (x, y))
            if w > 28 and h > 20:
                d.text((x + 3, y + 1), f'T{t["tile"]}', font=self.small, fill='#73909f')
        d.rectangle((0, self.height - 5, round(self.width * min(1, progress)), self.height), fill='#61d9b0')
        return frame


def ffmpeg_executable(override=None):
    if override:
        exe = shutil.which(override)
        if not exe:
            raise ValueError(f'FFmpeg not found: {override}')
        return exe
    if os.environ.get('IMAGEIO_FFMPEG_EXE'):
        return ffmpeg_executable(os.environ['IMAGEIO_FFMPEG_EXE'])
    exe = shutil.which('ffmpeg')
    if exe:
        return exe
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


class Encoder:
    def __init__(self, path, width, height, fps, ffmpeg=None, crf=18, threads=4):
        self.log = tempfile.TemporaryFile()
        cmd = [ffmpeg_executable(ffmpeg), '-hide_banner', '-loglevel', 'error', '-y',
               '-f', 'rawvideo', '-pixel_format', 'rgb24', '-video_size', f'{width}x{height}',
               '-framerate', str(fps), '-i', '-', '-an', '-c:v', 'libx264',
               '-preset', 'veryfast', '-crf', str(crf), '-threads', str(threads),
               '-pix_fmt', 'yuv420p', '-movflags', '+faststart', '-f', 'mp4', str(path)]
        try:
            self.process = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=self.log)
        except BaseException:
            self.log.close()
            raise

    def write(self, frame):
        try:
            self.process.stdin.write(frame.tobytes())
        except BrokenPipeError as exc:
            self.process.wait()
            self.log.seek(0)
            raise RuntimeError('FFmpeg failed: ' + self.log.read().decode(errors='replace')) from exc

    def close(self, abort=False):
        try:
            if abort and self.process.poll() is None:
                self.process.terminate()
            try:
                self.process.stdin.close()
            except BrokenPipeError:
                pass
            try:
                code = self.process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
                raise RuntimeError('FFmpeg did not terminate within 30 seconds')
            if code and not abort:
                self.log.seek(0)
                raise RuntimeError('FFmpeg failed: ' + self.log.read().decode(errors='replace'))
        finally:
            self.log.close()


def render_movie(timeline, path, *, width=1920, height=1080, fps=30, speed=1,
                 decay=.35, start=0, duration=None, tail=1, mode='all',
                 trigger='none', beam_iog=5, light_iog=6, post_us=190,
                 timezone_name='America/Chicago', ffmpeg=None, crf=18, threads=4, title=None):
    if start > timeline.duration:
        raise ValueError('--start is beyond the available detector timeline')
    end = timeline.duration if duration is None else min(timeline.duration, start + duration)
    # An endpoint frame ensures the final hit is actually visible, even if the
    # data end exactly on a frame boundary. Tail is measured in movie seconds.
    frames = math.ceil((end - start) / speed * fps) + 1 + math.ceil(tail * fps)
    replay = Replay(timeline, trigger=trigger, beam_iog=beam_iog, light_iog=light_iog, post_us=post_us)
    if trigger != 'none' and not len(replay.triggers):
        LOG.warning('No valid selected %s triggers in this file', trigger)
    encoder = None
    try:
        painter = Painter(timeline, width, height, speed=speed, decay=decay, mode=mode,
                          trigger=trigger, timezone_name=timezone_name, title=title)
        encoder = Encoder(path, width, height, fps, ffmpeg, crf, threads)
        for index in range(frames):
            seconds = start + index * speed / fps
            replay.advance(min(seconds, end))
            encoder.write(painter.frame(replay, seconds, (index + 1) / frames))
            if index % max(1, fps * 5) == 0:
                LOG.info('Rendering frame %d/%d (%.1f%%)', index + 1, frames, 100 * (index + 1) / frames)
        encoder.close()
        encoder = None
        return dict(frames=frames, movie_seconds=frames / fps, detector_start_seconds=start,
                    detector_end_seconds=end, replayed_hits=replay.applied, trigger_window_hits=replay.matched,
                    selected_triggers=len(replay.triggers))
    finally:
        replay.close()
        if encoder is not None and not encoder.log.closed:
            encoder.close(abort=True)
