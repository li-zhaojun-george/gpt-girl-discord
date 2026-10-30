"""Bounded animation sampling in a disposable, timed renderer process.

The model sees one chronological contact sheet per original asset, not a video.
No URLs, image filenames or expressions from Lottie are resolved or executed.
"""
import asyncio
import base64
import bisect
import io
import json
import math
import os
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageOps, ImageStat

MAX_BYTES = 10 * 1024 * 1024
MAX_PIXELS = 20_000_000
MAX_FRAMES = 600
MAX_TOTAL_PIXELS = 200_000_000
MAX_DURATION_MS = 30_000
SAMPLES = 6
CELL = 512
LABEL = 28
RENDER_TIMEOUT = 12


def flatten(frame, side=2048):
    frame = ImageOps.exif_transpose(frame).convert('RGBA')
    white = Image.new('RGBA', frame.size, 'white')
    white.alpha_composite(frame)
    frame = white.convert('RGB')
    frame.thumbnail((side, side), Image.Resampling.LANCZOS)
    return frame


def png(frame):
    out = io.BytesIO()
    frame.save(out, format='PNG')
    if out.tell() > MAX_BYTES:
        raise ValueError('Normalized image too large')
    return out.getvalue()


def normalize_static(data):
    if not data or len(data) > MAX_BYTES:
        raise ValueError('Invalid image size')
    with Image.open(io.BytesIO(data)) as source:
        if source.format not in {'PNG', 'JPEG', 'WEBP', 'GIF'}:
            raise ValueError('Unsupported image format')
        if source.width * source.height > MAX_PIXELS:
            raise ValueError('Image pixel limit exceeded')
        if getattr(source, 'n_frames', 1) != 1:
            raise ValueError('Static normalization cannot discard animation')
        source.load()
        return png(flatten(source))


def select_frames(durations, changes):
    """Include beginning/end, temporal coverage, and the strongest missed change."""
    count = len(durations)
    if count <= SAMPLES:
        return list(range(count))
    ends, elapsed = [], 0
    for duration in durations:
        elapsed += duration
        ends.append(elapsed)
    selected = {0, count-1}
    for ratio in (0.25, 0.5, 0.75):
        selected.add(min(count-1, bisect.bisect_right(ends, elapsed*ratio)))
    for index in sorted(range(count), key=lambda i: (-changes[i], i)):
        if len(selected) >= SAMPLES:
            break
        selected.add(index)
    return sorted(selected)


def difference(frame, previous):
    thumbnail = frame.convert('RGB').resize((64, 64))
    score = sum(ImageStat.Stat(ImageChops.difference(thumbnail, previous)).mean) if previous else 0
    return thumbnail, score


def sheet(frames, indices, durations, fmt, *, estimated=False, loop=None):
    columns = min(3, len(frames))
    rows = math.ceil(len(frames)/columns)
    canvas = Image.new('RGB', (columns*CELL, rows*(CELL+LABEL)), '#eeeeee')
    draw = ImageDraw.Draw(canvas)
    starts, elapsed = [], 0
    for duration in durations:
        starts.append(elapsed)
        elapsed += duration
    samples = []
    for position, (frame, index) in enumerate(zip(frames, indices)):
        x, y = (position % columns)*CELL, (position//columns)*(CELL+LABEL)
        frame = flatten(frame, CELL)
        canvas.paste(frame, (x+(CELL-frame.width)//2, y+LABEL+(CELL-frame.height)//2))
        draw.text((x+8, y+6), f'#{index+1} | {starts[index]/1000:.3f}s', fill='black')
        samples.append({'index': index, 'start_ms': round(starts[index], 3),
                        'duration_ms': round(durations[index], 3)})
    return png(canvas), {'animated': True, 'format': fmt, 'frame_count': len(durations),
        'duration_ms': round(elapsed, 3), 'sampled_frames': samples, 'sampled': len(indices)<len(durations),
        'timing_estimated': estimated, 'loop': loop, 'layout': {'columns': columns, 'rows': rows},
        'coverage': 'one_cycle_samples_no_audio'}


def raster(data):
    with Image.open(io.BytesIO(data)) as source:
        fmt = source.format
        if fmt not in {'PNG', 'JPEG', 'WEBP', 'GIF'}:
            raise ValueError('Unsupported image format')
        if source.width*source.height > MAX_PIXELS:
            raise ValueError('Image pixel limit exceeded')
        count = getattr(source, 'n_frames', 1)
        first = 1 if source.info.get('default_image') else 0
        count -= first  # An APNG poster is not its first animation frame.
        if count <= 0 or count > MAX_FRAMES or source.width*source.height*count > MAX_TOTAL_PIXELS:
            raise ValueError('Animation decode limit exceeded')
        if count == 1 and first == 0:
            return normalize_static(data), {'animated': False, 'format': fmt, 'frame_count': 1}
        durations, changes, previous = [], [], None
        estimated, elapsed = False, 0
        loop = source.info.get('loop')
        for index in range(first, first+count):
            source.seek(index)
            source.load()  # Pillow applies GIF/APNG disposal and compositing.
            duration = source.info.get('duration')
            if not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0:
                duration, estimated = 100, True
            elapsed += duration
            if elapsed > MAX_DURATION_MS:
                raise ValueError('Animation duration limit exceeded')
            durations.append(duration)
            previous, change = difference(flatten(source, 64), previous)
            changes.append(change)
        indices = select_frames(durations, changes)
        frames = []
        for index in indices:
            source.seek(index+first)
            frames.append(source.convert('RGBA').copy())
        return sheet(frames, indices, durations, 'APNG' if fmt == 'PNG' else fmt,
                     estimated=estimated, loop=loop)


def lottie_document(data):
    if len(data) > 2*1024*1024:
        raise ValueError('Lottie JSON too large')
    doc = json.loads(data)
    if not isinstance(doc, dict):
        raise ValueError('Invalid Lottie document')
    stack, nodes = [(doc, 0)], 0
    while stack:
        obj, depth = stack.pop()
        nodes += 1
        if depth > 40 or nodes > 150_000:
            raise ValueError('Lottie complexity limit exceeded')
        if isinstance(obj, dict):
            # Only self-contained vector stickers. No images/files/fonts,
            # expressions, or repeater expansion inside the native renderer.
            if ('u' in obj or 'fonts' in obj or 'fPath' in obj
                    or isinstance(obj.get('p'), str) or isinstance(obj.get('x'), str)
                    or obj.get('ty') in (2, 5, 'rp')):
                raise ValueError('Lottie external assets/text/expressions/repeaters unsupported')
            stack.extend((v, depth+1) for v in obj.values())
        elif isinstance(obj, list):
            if len(obj) > 5000:
                raise ValueError('Lottie array too large')
            stack.extend((v, depth+1) for v in obj)
        elif isinstance(obj, (int, float)) and (not math.isfinite(obj) or abs(obj)>1_000_000):
            raise ValueError('Invalid Lottie number')
    for field in ('w', 'h', 'fr', 'ip', 'op'):
        if type(doc.get(field)) not in (int, float):
            raise ValueError('Missing Lottie dimensions/timing')
    if not (0 < doc['w'] <= 1024 and 0 < doc['h'] <= 1024 and 0 < doc['fr'] <= 120):
        raise ValueError('Lottie dimensions/timing limit exceeded')
    count = math.ceil(doc['op']-doc['ip'])
    if not 1 <= count <= MAX_FRAMES or count/doc['fr']*1000 > MAX_DURATION_MS:
        raise ValueError('Lottie duration/frame limit exceeded')
    if not isinstance(doc.get('layers'), list) or len(doc['layers'])>100:
        raise ValueError('Invalid Lottie layers')
    # Reject recursive/missing precompositions before invoking the renderer.
    assets = doc.get('assets', [])
    if not isinstance(assets, list) or len(assets)>100:
        raise ValueError('Invalid Lottie assets')
    table = {}
    for asset in assets:
        if not isinstance(asset, dict) or not isinstance(asset.get('id'), str) or asset['id'] in table:
            raise ValueError('Invalid Lottie asset')
        table[asset['id']] = asset
    def visit(obj, path=()):
        if isinstance(obj, dict):
            ref = obj.get('refId')
            if ref is not None:
                if not isinstance(ref, str) or ref not in table or ref in path or len(path)>10:
                    raise ValueError('Invalid recursive Lottie asset')
                visit(table[ref], (*path, ref))
            for value in obj.values():
                visit(value, path)
        elif isinstance(obj, list):
            for value in obj:
                visit(value, path)
    visit(doc['layers'])
    return doc, count


def lottie(data):
    doc, count = lottie_document(data)
    from rlottie_python import LottieAnimation
    animation = LottieAnimation.from_data(json.dumps(doc), resource_path='')
    actual = int(animation.lottie_animation_get_totalframe())
    if not count <= actual <= count+1:
        raise ValueError('Lottie renderer frame mismatch')
    # Lottie's op is exclusive; rlottie may also expose the terminal boundary.
    actual = count
    # Scan small rendered frames for a brief change, render selected frames at
    # native sticker resolution. No JSON descriptions are used as visual facts.
    durations = [1000/doc['fr']]*actual
    changes, previous = [], None
    for index in range(actual):
        frame = animation.render_pillow_frame(frame_num=index, width=64, height=64)
        previous, change = difference(flatten(frame, 64), previous)
        changes.append(change)
    indices = select_frames(durations, changes)
    frames = [animation.render_pillow_frame(frame_num=i, width=int(doc['w']), height=int(doc['h'])) for i in indices]
    return sheet(frames, indices, durations, 'LOTTIE')


def render(data, is_lottie=False):
    if not data or len(data)>MAX_BYTES:
        raise ValueError('Invalid image size')
    return lottie(data) if is_lottie else raster(data)


async def prepare_media(data, is_lottie=False):
    if not data or len(data)>MAX_BYTES:
        raise ValueError('Invalid image size')
    process = None
    try:
        env = {k:v for k,v in os.environ.items() if k.upper() in {'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP'}}
        process = await asyncio.create_subprocess_exec(sys.executable, '-I', '-B', str(Path(__file__).resolve()),
            'lottie' if is_lottie else 'raster', stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, env=env,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        output, _ = await asyncio.wait_for(process.communicate(data), RENDER_TIMEOUT)
        if process.returncode or len(output)>15*1024*1024:
            raise ValueError('Animation render failed or exceeded limits')
        result = json.loads(output)
        pixels = base64.b64decode(result['png'], validate=True)
        if len(pixels)>MAX_BYTES or not pixels.startswith(b'\x89PNG\r\n\x1a\n'):
            raise ValueError('Invalid renderer output')
        return pixels, result['animation']
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()


if __name__ == '__main__':
    try:
        pixels, metadata = render(sys.stdin.buffer.read(MAX_BYTES+1), sys.argv[1]=='lottie')
        sys.stdout.write(json.dumps({'png': base64.b64encode(pixels).decode('ascii'), 'animation': metadata}))
    except Exception:
        sys.exit(2)
