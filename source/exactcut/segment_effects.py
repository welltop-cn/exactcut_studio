"""Frame-anchored segment decorations; no changes to source cuts or audio."""
from fractions import Fraction
from pathlib import Path

from PIL import Image, ImageDraw

from .errors import ValidationError
from .fonts import font_path, load_font
from .media import ffprobe, run_checked
from .util import file_sha256, json_sha256

EFFECT_VERSION = 3
CUT_FIELDS = ('type', 'episode', 'start_ms', 'end_ms', 'text', 'duration_ms', 'full_episode', 'to_end')


def cut_timeline(timeline):
    # Preserve the exact old shape, including absent fields versus explicit null.
    return [{key: item[key] for key in CUT_FIELDS if key in item} for item in timeline]


def has_effects(timeline):
    return any(item.get('segment_caption') or item.get('transition_in') == 'flash_white' for item in timeline)


def video_frames(path, fps):
    video = next(s for s in ffprobe(path)['streams'] if s['codec_type'] == 'video')
    if (not str(video.get('nb_frames', '')).isdigit()
            or Fraction(video['r_frame_rate']) != Fraction(str(fps))):
        raise ValidationError('无法核对片段效果的实际帧边界，请重新制作')
    return int(video['nb_frames'])


def effect_plan(timeline, lengths, fps):
    if len(timeline) != len(lengths) or any(type(n) is not int or n <= 0 for n in lengths):
        raise ValidationError('片段实际帧边界缺失，请重新制作')
    rate = Fraction(str(fps))
    captions, flashes, cursor = [], [], 0
    for index, (item, count) in enumerate(zip(timeline, lengths)):
        caption = item.get('segment_caption')
        if caption:
            wanted = max(1, round(Fraction(caption['duration_ms'], 1000) * rate))
            captions.append({'segment': index + 1, 'text': caption['text'],
                             'requested_duration_ms': caption['duration_ms'],
                             'start_frame': cursor, 'end_frame': cursor + min(count, wanted)})
        if item.get('transition_in') == 'flash_white':
            if not index:
                raise ValidationError('第一段不能设置入场闪白')
            total = round(Fraction(item.get('transition_duration_ms', 500), 1000) * rate)
            before = min(total // 2, (lengths[index - 1] - 1) // 2)
            after = min(total - total // 2, (count - 1) // 2)
            if min(before, after) < 2:
                raise ValidationError(f'片段 {index + 1} 的相邻片段太短，不能启用闪白')
            flashes.append({'boundary_frame': cursor, 'before_frames': before, 'after_frames': after})
        cursor += count
    return {'version': EFFECT_VERSION, 'fps': str(fps), 'total_frames': cursor,
            'segment_frames': list(lengths), 'captions': captions, 'flashes': flashes}


def caption_image(text, width, height):
    """Fixed plain caption, up to three lines, measured with the bundled CJK font."""
    size = max(8, round(width * 72 / 1080))
    stroke = max(1, round(width * .002))
    font = load_font(font_path('title_card'), size)
    image = Image.new('RGBA', (width, height))
    draw = ImageDraw.Draw(image)
    lines = text.split('\n')
    if len(lines) > 3:
        raise ValidationError('本段字幕最多三行')
    boxes = [draw.textbbox((0, 0), line, font=font, stroke_width=stroke) for line in lines]
    if any(box[2] - box[0] > width * .86 for box in boxes):
        raise ValidationError('本段字幕过长，请精简文案或分为两行')
    gap = max(2, round(size * .3))
    block = sum(box[3] - box[1] for box in boxes) + gap * (len(lines) - 1)
    y = round(height * .25 - block / 2)
    for line, box in zip(lines, boxes):
        draw.text(((width - box[2] + box[0]) / 2 - box[0], y - box[1]), line,
                  font=font, fill='white', stroke_width=stroke, stroke_fill='black')
        y += box[3] - box[1] + gap
    return image


def apply_effects(source, output, render, work, plan, *, hook=None, hook_end=0, hook_fade=0):
    fps = Fraction(str(render['fps']))
    width, height = int(render['width']), int(render['height'])
    command = ['ffmpeg', '-y', '-v', 'error', '-i', str(source)]
    chains, label, inputs = [], '0:v', 0

    def add_overlay(png, start, count, filters=''):
        nonlocal inputs, label
        inputs += 1
        duration = float(Fraction(count, 1) / fps)
        command.extend(['-loop', '1', '-framerate', str(render['fps']), '-t', f'{duration:.9f}', '-i', str(png)])
        stream = f'overlay{inputs}'
        chains.append(f'[{inputs}:v]format=rgba{filters},setpts=PTS+{start}/({render["fps"]}*TB)[{stream}]')
        next_label = f'layer{inputs}'
        chains.append(f'[{label}][{stream}]overlay=0:0:format=auto:eof_action=pass:repeatlast=0:'
                      f"enable='gte(n,{start})*lt(n,{start + count})'[{next_label}]")
        label = next_label

    if plan['flashes']:
        white = work/'segment-white.png'
        Image.new('RGBA', (width, height), 'white').save(white)
        for flash in plan['flashes']:
            before, after = flash['before_frames'], flash['after_frames']
            add_overlay(white, flash['boundary_frame'] - before, before + after,
                        f',fade=t=in:s=0:n={before - 1}:alpha=1,fade=t=out:s={before}:n={after - 1}:alpha=1')
    warnings = []
    hook_box = None
    if hook:
        with Image.open(hook) as image:
            hook_box = image.getbbox()
    for index, caption in enumerate(plan['captions']):
        image = caption_image(caption['text'], width, height)
        box = image.getbbox()
        if hook_box and box and caption['start_frame'] < round(hook_end * fps):
            if max(box[0], hook_box[0]) < min(box[2], hook_box[2]) and max(box[1], hook_box[1]) < min(box[3], hook_box[3]):
                warnings.append(f'片段 {caption["segment"]} 的字幕与开头字幕重叠，请调整开头字幕位置或时长')
        png = work/f'segment-caption-{index}.png'
        image.save(png)
        add_overlay(png, caption['start_frame'], caption['end_frame'] - caption['start_frame'])
    if hook:
        filters = f',fade=t=out:st={hook_end - hook_fade:.6f}:d={hook_fade:.6f}:alpha=1' if hook_fade else ''
        add_overlay(hook, 0, max(1, round(hook_end * fps)), filters)
    chains.append(f'[{label}]format=yuv420p[v]')
    command += ['-filter_complex', ';'.join(chains), '-map', '[v]', '-map', '0:a:0?',
                '-fps_mode', 'passthrough', '-frames:v', str(plan['total_frames']),
                '-c:v', render['video_codec'], '-preset', render['preset'], '-crf', str(render['crf']),
                '-c:a', 'copy', '-movflags', '+faststart', str(output)]
    run_checked(command)
    if video_frames(output, render['fps']) != plan['total_frames']:
        raise ValidationError('字幕/闪白合成后帧数变化，未生成成片')
    plan['warnings'] = warnings
    plan['identity'] = json_sha256({'plan': {k: v for k, v in plan.items() if k != 'identity'},
                                    'base_sha256': file_sha256(source), 'render': render,
                                    'font_sha256': file_sha256(Path(font_path('title_card')))})
