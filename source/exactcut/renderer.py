from __future__ import annotations

import json
import fcntl
import os
import time
import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from .errors import ValidationError
from .fonts import font_path, load_font
from .media import resolve_episode_video, run_checked, duration_ms, ffprobe
from .source_cache import hold_shared_for, shared_for
from .hook_visual import resolve_hook_visual, render_personalized_hook_png
from .hook_emphasis import resolve_emphasis, manual_emphasis, plain_hook
from .runtime import environment, protect_path
from .hook_options import HookOptions, approved_emphasis, hook_duration
from .hook_presentation import presentation, sample_timeline_frames, approved_preview
from .util import file_sha256, json_sha256, read_json, write_json_atomic
from . import diagnostics as diag


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    return load_font(font_path('title_card'), size)


def _draw_centered_text(image: Image.Image, text: str, *, y: int, font_size: int, max_chars: int = 28) -> None:
    draw = ImageDraw.Draw(image)
    font = _font(font_size)
    lines = textwrap.wrap(text.strip(), width=max_chars) or [text]
    boxes = [draw.textbbox((0, 0), line, font=font, stroke_width=2) for line in lines]
    heights = [box[3] - box[1] for box in boxes]
    total_height = sum(heights) + max(0, len(lines) - 1) * 18
    cursor = y - total_height // 2
    for line, box, height in zip(lines, boxes, heights):
        width = box[2] - box[0]
        draw.text(((image.width - width) // 2, cursor), line, font=font, fill="white", stroke_width=4, stroke_fill="black")
        cursor += height + 18


def title_card_image(text: str, width: int, height: int) -> Image.Image:
    image = Image.new("RGB", (width, height), "black")
    _draw_centered_text(image, text, y=height // 2, font_size=64, max_chars=26)
    return image


def _make_card_png(path: Path, text: str, width: int, height: int) -> None:
    title_card_image(text, width, height).save(path)


def _render_episode_unit(source: Path, start_ms: int, end_ms: int, output: Path, render: dict[str, Any]) -> None:
    duration = (end_ms - start_ms) / 1000
    if duration <= 0:
        raise ValidationError("渲染片段时长必须大于0")
    vf = (
        f"scale={render['width']}:{render['height']}:force_original_aspect_ratio=increase,"
        f"crop={render['width']}:{render['height']},setsar=1,fps={render['fps']},format=yuv420p"
    )
    run_checked([
        "ffmpeg", "-y", "-v", "error", "-ss", f"{start_ms / 1000:.3f}", "-accurate_seek",
        "-i", str(source), "-t", f"{duration:.3f}", "-map", "0:v:0", "-map", "0:a:0?",
        "-vf", vf, "-af", "aresample=async=1:first_pts=0", "-c:v", render["video_codec"],
        "-preset", render["preset"], "-crf", str(render["crf"]), "-pix_fmt", "yuv420p",
        "-c:a", render["audio_codec"], "-ar", str(render["audio_rate"]), "-ac", str(render["audio_channels"]),
        "-movflags", "+faststart", str(output),
    ])


def _render_card_unit(text: str, duration_ms: int, output: Path, render: dict[str, Any], work: Path) -> None:
    png = work / f"{output.stem}.png"
    _make_card_png(png, text, int(render["width"]), int(render["height"]))
    _render_still_unit(png, duration_ms, output, render)


def _render_still_unit(png: Path, duration_ms: int, output: Path, render: dict[str, Any], *, sar: str | None = None) -> None:
    duration = duration_ms / 1000
    vf = (f'setsar={sar},' if sar else '') + 'format=yuv420p'
    run_checked([
        "ffmpeg", "-y", "-v", "error", "-loop", "1", "-framerate", str(render["fps"]), "-i", str(png),
        "-f", "lavfi", "-i", f"anullsrc=r={render['audio_rate']}:cl=stereo", "-t", f"{duration:.3f}",
        "-map", "0:v:0", "-map", "1:a:0", "-vf", vf, "-c:v", render["video_codec"],
        "-preset", render["preset"], "-crf", str(render["crf"]), "-c:a", render["audio_codec"],
        "-ar", str(render["audio_rate"]), "-ac", str(render["audio_channels"]), str(output),
    ])


def _concat_units(inputs: list[Path], output: Path, render: dict[str, Any], expected_ms: int) -> None:
    # Units have already been normalized and independently checked. Copy their
    # video packets; reconstruct audio at frame boundaries to avoid accumulating
    # AAC padding or container-duration rounding at each join.
    from fractions import Fraction
    videos = [next(s for s in ffprobe(item)['streams'] if s['codec_type'] == 'video') for item in inputs]
    fields = ('codec_name', 'profile', 'level', 'width', 'height', 'pix_fmt',
              'time_base', 'r_frame_rate', 'sample_aspect_ratio')
    signature = tuple(videos[0].get(key) for key in fields)
    if (videos[0].get('codec_name') != 'h264'
            or Fraction(videos[0]['r_frame_rate']) != Fraction(str(render['fps']))
            or any(int(video.get('has_b_frames', 0)) > 2 for video in videos)
            or any(tuple(video.get(key) for key in fields) != signature for video in videos)
            or any(not str(video.get('nb_frames', '')).isdigit() for video in videos)):
        return _concat_units_encoded(inputs, output, render, expected_ms)
    lengths = [int(video['nb_frames']) / Fraction(video['r_frame_rate']) for video in videos]
    with tempfile.TemporaryDirectory(prefix='join-', dir=output.parent) as directory:
        listing = Path(directory) / 'video.ffconcat'
        lines = ['ffconcat version 1.0']
        for index, (item, length) in enumerate(zip(inputs, lengths)):
            video_only = Path(directory) / f'{index}.mp4'
            run_checked(['ffmpeg', '-y', '-v', 'error', '-i', str(item),
                         '-map', '0:v:0', '-c:v', 'copy', '-an', str(video_only)])
            item = video_only
            quoted = str(item.resolve()).replace("'", "'\\''")
            lines += [f"file '{quoted}'", f'duration {float(length):.9f}']
        listing.write_text('\n'.join(lines) + '\n')
        command = ['ffmpeg', '-y', '-v', 'error', '-f', 'concat', '-safe', '0', '-i', str(listing)]
        chains = []
        for index, (item, length) in enumerate(zip(inputs, lengths), 1):
            command += ['-i', str(item)]
            chains.append(f'[{index}:a]aresample={render["audio_rate"]},apad,'
                          f'atrim=duration={float(length):.9f},asetpts=PTS-STARTPTS[a{index}]')
        chains.append(''.join(f'[a{i}]' for i in range(1, len(inputs)+1)) +
                      f'concat=n={len(inputs)}:v=0:a=1[a]')
        command += ['-filter_complex', ';'.join(chains), '-map', '0:v:0', '-map', '[a]',
                    # Preserve presentation order while making decoding timestamps continuous
                    # across independent GOPs (including one-frame cuts).
                    '-c:v', 'copy', '-bsf:v', f'setts=pts=PTS:dts=(N-2)/({render["fps"]}*TB)', '-c:a', render['audio_codec'], '-ar', str(render['audio_rate']),
                    '-ac', str(render['audio_channels']), '-movflags', '+faststart', str(output)]
        run_checked(command)


def _concat_units_encoded(inputs: list[Path], output: Path, render: dict[str, Any], expected_ms: int) -> None:
    command = ["ffmpeg", "-y", "-v", "error"]
    for item in inputs:
        command += ["-i", str(item)]
    chains = []
    labels = []
    for index in range(len(inputs)):
        chains.append(f"[{index}:v]setpts=PTS-STARTPTS[v{index}]")
        chains.append(f"[{index}:a]aresample={render['audio_rate']},asetpts=PTS-STARTPTS[a{index}]")
        labels.append(f"[v{index}][a{index}]")
    chains.append(f"{''.join(labels)}concat=n={len(inputs)}:v=1:a=1[joined][a];[joined]fps={render['fps']},setpts=N/({render['fps']}*TB)[v]")
    command += [
        "-filter_complex", ";".join(chains), "-map", "[v]", "-map", "[a]",
        "-r", str(render["fps"]), "-t", f"{expected_ms / 1000:.3f}",
        "-c:v", render["video_codec"], "-preset", render["preset"], "-crf", str(render["crf"]),
        "-pix_fmt", "yuv420p", "-c:a", render["audio_codec"], "-ar", str(render["audio_rate"]),
        "-ac", str(render["audio_channels"]), "-movflags", "+faststart", str(output),
    ]
    run_checked(command)


def _apply_hook(input_video: Path, hook_text: str, output: Path, render: dict[str, Any], work: Path,
                emphasis: dict[str, Any] | None = None, options: HookOptions | None = None,
                frames: list[bytes] | None = None, cache_dir: Path | None = None,
                preview_decision: dict | None = None, normalized_input: bool = False) -> dict[str, Any] | None:
    if not hook_text.strip():
        shutil.copy2(input_video, output)
        return None
    if preview_decision is not None:
        decision = dict(preview_decision)
        overlay = render_personalized_hook_png(hook_text,work,decision,
            video_w=int(render['width']),video_h=int(render['height']))
        decision['preview_approved'] = True
    elif options is not None:
        overlay, decision = presentation(hook_text, emphasis, options, frames or [], work,
                                         cache_dir or work/'placement', int(render['width']), int(render['height']))
    else:
        style = str(render.get("hook_style", "auto"))
        decision = resolve_hook_visual({"hook_text": hook_text}, input_video, work,
            forced_style=None if style == "auto" else style, emphasis=emphasis)
        overlay = render_personalized_hook_png(hook_text, work, decision,
            video_w=int(render["width"]), video_h=int(render["height"]))
    total_ms = duration_ms(input_video)
    total_s = total_ms / 1000
    end_s = (hook_duration(hook_text, options, total_ms) if options is not None
             else min(total_ms, int(render["hook_duration_ms"]))) / 1000
    fade_s = min(end_s, int(render.get("hook_fade_out_ms", 1000)) / 1000)
    if end_s <= 0 or fade_s < 0:
        raise ValidationError("Hook 时长必须大于零，淡出时长不能为负")
    overlay_filter = "[1:v]format=rgba"
    if fade_s:
        overlay_filter += f",fade=t=out:st={end_s - fade_s:.3f}:d={fade_s:.3f}:alpha=1"
    overlay_filter += "[ov];"
    command = [
        "ffmpeg", "-y", "-v", "error", "-i", str(input_video),
        # Invisible PNG frames otherwise keep decoding and fading for the entire film.
        # The existing eof_action=pass retains all main-video frames after the Hook.
        "-loop", "1", "-framerate", str(render["fps"]), "-t", f"{end_s:.3f}", "-i", str(overlay),
        "-filter_complex", overlay_filter + f"[0:v][ov]overlay=0:0:format=auto:eof_action=pass:enable='lt(t,{end_s:.3f})',fps={render['fps']},setpts=N/({render['fps']}*TB),format=yuv420p[v]",
        "-map", "[v]", "-map", "0:a:0?", "-r", str(render["fps"]), "-t", f"{total_s:.3f}",
        "-c:v", render["video_codec"], "-preset", render["preset"],
        "-crf", str(render["crf"]), "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart", str(output),
    ]
    from .hook_splice import try_hook_splice
    if not normalized_input or not try_hook_splice(input_video, output, render, end_s, command):
        run_checked(command)
    return {**decision, "duration_ms": round(end_s * 1000), "fade_out_ms": round(fade_s * 1000)}


def source_fingerprints(project_dir: Path, timeline: list[dict], source_cache=None) -> list[dict]:
    sources = {}
    for item in timeline:
        if item['type'] != 'episode' or item['episode'] in sources:
            continue
        source = resolve_episode_video(project_dir, item['episode'])
        with shared_for(source):
            protect_path(source)
            stat = source.stat()
            identity = (stat.st_ino, stat.st_size, stat.st_mtime_ns)
            cached = (source_cache or {}).get(str(source))
            digest = cached['sha256'] if cached and cached['identity'] == identity else file_sha256(source)
        if source_cache is not None:
            source_cache[str(source)] = {'identity':identity,'sha256':digest}
        sources[item['episode']] = {'episode':item['episode'], 'sha256':digest}
    return list(sources.values())


@diag.traced('render')
def render_request(project_dir: Path, request: Any, safe_timeline: list[dict[str, Any]], output: Path,
                   config: dict[str, Any], *, source_cache=None, progress=None) -> dict[str, Any]:
    def step(name, **details):
        diag.event('render.step', request_id=getattr(request, 'request_id', None), drama_id=project_dir.name, stage=name, **details)
        if progress:
            progress(name, **details)
    step('storage_check')
    render = config["render"]
    diag.event('render.plan', request_id=getattr(request, 'request_id', None), drama_id=project_dir.name,
               hook_characters=len(request.hook_text), config_sha256=json_sha256(config),
               segments=len(safe_timeline), fps=render['fps'], width=render['width'], height=render['height'])
    for index, item in enumerate(safe_timeline):
        diag.event('render.segment_plan', request_id=getattr(request, 'request_id', None), segment=index+1,
                   **{k: item.get(k) for k in ('type', 'episode', 'start_ms', 'end_ms', 'duration_ms')})
    protect_path(project_dir)
    protect_path(output)
    from .storage import prepare_render
    prepare_render(project_dir,output,safe_timeline,render)
    output.parent.mkdir(parents=True, exist_ok=True)
    timings = {}
    started = time.monotonic()
    emphasis = None
    options = getattr(request, 'hook_options', None)
    step('source_check')
    sources = source_fingerprints(project_dir,safe_timeline,source_cache)
    preview_decision = approved_preview(project_dir,request,safe_timeline,sources) if options is not None else None
    if request.hook_text.strip() and render.get('hook_emphasis') == 'ai_semantic_v1':
        step('ai_emphasis')
        if preview_decision is not None:
            emphasis = preview_decision['emphasis']
        elif options is not None:
            emphasis = approved_emphasis(request.hook_text, options, project_dir/'cache/hook-emphasis')
        elif environment() == 'production':
            emphasis = resolve_emphasis(request.hook_text, project_dir/'cache/hook-emphasis')
        else:
            emphasis = manual_emphasis(request.hook_text) or {
                'source':'isolated_disabled','text':plain_hook(request.hook_text),'highlights':[],
                'reason':'隔离环境未调用 AI；正式环境需要语义分析结果','style':'default','version':1}
    timings['ai_emphasis'] = round(time.monotonic()-started, 3)
    # Hook-only edits and renaming reuse the checked cut; source bytes and encoder settings are part of the key.
    base_render = {k:v for k,v in render.items() if not k.startswith('hook_')}
    identity = {'version':3,'timeline':safe_timeline,'sources':sources,
                'render':base_render}
    key = json_sha256(identity)
    cache = project_dir/'cache/render-base'
    cache.mkdir(parents=True, exist_ok=True)
    base = cache/f'{key}.mp4'
    meta_path = cache/f'{key}.json'
    with tempfile.TemporaryDirectory(prefix=f"exactcut-{request.request_id}-") as temporary:
        work = Path(temporary)
        started = time.monotonic()
        with (cache/f'{key}.lock').open('a') as lock:
            step('base_cache_check')
            lock_started = time.monotonic()
            fcntl.flock(lock, fcntl.LOCK_EX)
            diag.event('render.cache_lock_acquired', cache_key=key, wait_ms=round((time.monotonic()-lock_started)*1000))
            reused = False
            if base.exists() and meta_path.exists():
                try:
                    meta = read_json(meta_path)
                    reused = meta['identity'] == identity and meta['sha256'] == file_sha256(base)
                    reused = reused and abs(duration_ms(base)-meta['expected_duration_ms']) <= int(render['duration_tolerance_ms'])
                except (ValueError, KeyError, OSError, RuntimeError):
                    reused = False
            if not reused:
                units, boundaries_ms, unit_checks = [], [], []
                expected_ms = 0
                for index, item in enumerate(safe_timeline):
                    step('base_cut', segment=index+1, total=len(safe_timeline), completed=index)
                    unit = work/f'unit-{index:04d}.mp4'
                    if item['type'] == 'episode':
                        source = resolve_episode_video(project_dir,item['episode'])
                        hold_shared_for(source)
                        duration = int(item['end_ms'])-int(item['start_ms'])
                        _render_episode_unit(source,int(item['start_ms']),int(item['end_ms']),unit,render)
                    else:
                        duration = int(item['duration_ms'])
                        _render_card_unit(str(item['text']),duration,unit,render,work)
                    actual = duration_ms(unit)
                    tolerance = max(50,round(2000/float(render['fps'])))
                    diag.event('render.segment_qc', request_id=getattr(request, 'request_id', None), segment=index+1,
                               expected_ms=duration, actual_ms=actual, tolerance_ms=tolerance,
                               passed=abs(actual-duration)<=tolerance)
                    if abs(actual-duration)>tolerance:
                        raise ValidationError(f'片段 {index+1} 时长不符：预期 {duration}ms，实际 {actual}ms；未进入拼接')
                    unit_checks.append({'segment':index+1,'expected_ms':duration,'actual_ms':actual})
                    expected_ms += duration
                    if index+1 < len(safe_timeline):
                        boundaries_ms.append(expected_ms)
                    units.append(unit)
                    step('base_cut', segment=index+1, total=len(safe_timeline), completed=index+1)
                joined = work/'joined.mp4'
                step('concat', completed=len(units), total=len(units))
                _concat_units(units,joined,render,expected_ms)
                if abs(duration_ms(joined)-expected_ms)>int(render['duration_tolerance_ms']):
                    raise ValidationError('拼接后的计划与实际时长不一致，未缓存')
                # Atomic publication on the cache filesystem; never link a writable original/output.
                temporary_base = cache/f'.{key}-{os.getpid()}.mp4'
                try:
                    shutil.copy2(joined,temporary_base)
                    os.replace(temporary_base,base)
                finally:
                    temporary_base.unlink(missing_ok=True)
                meta = {'identity':identity,'sha256':file_sha256(base),'expected_duration_ms':expected_ms,
                        'edit_boundaries_ms':boundaries_ms,'unit_checks':unit_checks}
                write_json_atomic(meta_path,meta)
            timings['base_cut'] = round(time.monotonic()-started,3)
            diag.event('render.cache_result', cache_key=key, reused=reused, expected_ms=meta['expected_duration_ms'])
            os.utime(base,None)  # LRU usage, updated while its lifetime lock is held.
            # Pin validated bytes in this request's work before cleanup can evict the cache.
            # Reuse the joined path so a fresh cut needs no additional temporary-video peak.
            request_base = work/'joined.mp4'
            shutil.copy2(base,request_base)
        frames = []
        coverage=None
        if options is not None and request.hook_text.strip() and preview_decision is None:
            step('frame_sampling')
            started = time.monotonic()
            visible=hook_duration(request.hook_text,options,meta['expected_duration_ms'])
            frames,coverage=sample_timeline_frames(project_dir,safe_timeline,work,visible,
                                                   width=render['width'],height=render['height'])
            timings['frame_sampling'] = round(time.monotonic()-started,3)
        started = time.monotonic()
        step('hook_overlay', base_cache_reused=reused)
        ending = getattr(request, 'ending', None)
        has_ending = ending is not None and ending.mode != 'none'
        hook_output = work/'hooked.mp4' if has_ending else output
        hook_visual = _apply_hook(request_base,request.hook_text,hook_output,render,work,emphasis=emphasis,
                                 options=options,frames=frames,cache_dir=project_dir/'cache/hook-placement',
                                 preview_decision=preview_decision, normalized_input=True)
        timings['hook_overlay'] = round(time.monotonic()-started,3)
        if hook_visual and coverage:hook_visual['sampling']=coverage
        expected_ms = meta['expected_duration_ms']
        boundaries = list(meta['edit_boundaries_ms'])
        checks = list(meta['unit_checks'])
        ending_visual = None
        if has_ending:
            from .ending import still_image
            started = time.monotonic()
            step('ending_overlay')
            png = still_image(project_dir, request_base, ending, render, work)
            unit = work/'ending.mp4'
            from fractions import Fraction
            video = next(s for s in ffprobe(hook_output)['streams'] if s['codec_type'] == 'video')
            sar = video.get('sample_aspect_ratio')
            sar = str(Fraction(sar.replace(':', '/'))) if sar and sar != 'N/A' else None
            _render_still_unit(png, ending.duration_ms, unit, render, sar=sar)
            actual_ms = duration_ms(unit)
            if abs(actual_ms-ending.duration_ms) > max(50, round(2000/float(render['fps']))):
                raise ValidationError('结尾时长不符，未进入拼接')
            boundaries.append(expected_ms)
            expected_ms += ending.duration_ms
            checks.append({'segment': len(safe_timeline)+1, 'type': 'ending',
                           'expected_ms': ending.duration_ms, 'actual_ms': actual_ms})
            _concat_units([hook_output, unit], output, render, expected_ms)
            if abs(duration_ms(output)-expected_ms) > int(render['duration_tolerance_ms']):
                raise ValidationError('追加结尾后的计划与实际时长不一致')
            ending_visual = {**ending.model_dump(mode='json'), 'start_ms': meta['expected_duration_ms'],
                             'image_sha256': file_sha256(png)}
            timings['ending_overlay'] = round(time.monotonic()-started, 3)
    from .storage import cleanup, data_root
    if project_dir.is_relative_to(data_root()):cleanup(data_root())
    return {'expected_duration_ms':expected_ms,'edit_boundaries_ms':boundaries,
            'unit_checks':checks,'timeline':safe_timeline,'hook_visual':hook_visual,'ending_visual':ending_visual,
            'base_cache_reused':reused,'base_cache_key':key,'stage_seconds':timings}
