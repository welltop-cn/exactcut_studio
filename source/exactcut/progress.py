"""Honest wall-clock observations; heartbeats never claim work completed."""
from datetime import datetime, timezone

STAGES = {'source_check': '校验原片', 'storage_check': '检查可用空间',
          'rendering': '准备渲染', 'ai_emphasis': 'AI 理解重点词',
          'base_cache_check': '校验剪辑缓存', 'base_cut': '剪辑片段',
          'concat': '拼接片段', 'frame_sampling': '抽样检查 Hook 遮挡',
          'hook_overlay': '排版与合成 Hook', 'qc': '格式与时长检查',
          'preview': '生成轻量预览', 'upload': '上传文件',
          'fbbm': '等待 FBBM 处理', 'download': '获取正片',
          'qc_passed': '已完成，待预览', 'failed': '制作失败', 'reused': '复用已检查成片'}


def observation(data, *, paused=False, now=None):
    now = now or datetime.now(timezone.utc)
    executing = bool(data.get('executor_running')) and not data.get('execution_ended_at')
    end = data.get('execution_ended_at') or (None if executing else data.get('failed_at'))
    if not executing and data.get('status') != 'processing':
        end = end or data.get('completed_at')
    def age(value, reference=now):
        if not value:
            return None
        try:
            return max(0, round((reference - datetime.fromisoformat(value)).total_seconds()))
        except (ValueError, TypeError):
            return None
    try:
        reference = datetime.fromisoformat(end) if end else now
    except (ValueError, TypeError):
        reference = now
    pulse_age = age(data.get('heartbeat_at'))
    progress_age = age(data.get('last_progress_at'), reference)
    live = bool(data.get('executor_running')) and not end
    return {'step': STAGES.get(data.get('stage'), ''),
            'elapsed_seconds': age(data.get('execution_started_at') or data.get('started_at'), reference),
            'stage_seconds': age(data.get('stage_started_at'), reference),
            'last_progress_seconds': progress_age, 'heartbeat_seconds': pulse_age if live else None,
            'details': data.get('progress_details') or {},
            'notice': '已暂停，进度已保留' if paused else
                '较久未收到执行心跳，请查看日志或刷新状态' if live and pulse_age is not None and pulse_age > 20 else
                '尚无新节点完成；本步骤可能需要较长时间' if live and progress_age is not None and progress_age > 60 else ''}
