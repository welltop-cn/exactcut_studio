"""Focused effective concurrency receipt for this change, not a config framework."""
from . import concurrency,editor_resources,mosaic_queue,preview_subtitles,media
from .mosaic_sam2 import CPU_THREADS


def snapshot():
    lanes=concurrency.settings();subtitles=preview_subtitles.settings()
    return {'ordinary':{key:lanes[key] for key in (*concurrency.LANES,'max_active_children','enabled')},
            'remaining_openai_budget':lanes['ai'],'interactive_execution':editor_resources.settings(),
            'mosaic_final':mosaic_queue.settings()['max_active_tasks'],
            'subtitles':{key:subtitles[key] for key in ('max_active_tasks','max_queued_tasks')},
            'ffmpeg_threads':media.encoder_limit(),'sam_threads':CPU_THREADS}


def require_equal(expected):
    actual=snapshot()
    differences=[key for key in sorted(set(expected)|set(actual)) if expected.get(key)!=actual.get(key)]
    if differences:raise ValueError('本次有效并发配置与候选生产基线不一致：'+', '.join(differences))
    return actual


if __name__=='__main__':
    import json,sys
    from pathlib import Path
    print(json.dumps(require_equal(json.loads(Path(sys.argv[1]).read_text())) if len(sys.argv)>1 else snapshot(),ensure_ascii=False,sort_keys=True))
