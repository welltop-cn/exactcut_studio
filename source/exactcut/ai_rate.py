"""One persistent, nonblocking request bucket shared by all AI callers."""
import fcntl
import math
import time
from .concurrency import ai_settings
from .runtime import data_root, protect_path
from .stage_results import ResourceDeferred
from .util import load_config, read_json, write_json_atomic
from . import diagnostics as diag


def acquire():
    # This is a conservative operator budget, not a verified vendor quota.
    limits = ai_settings(load_config().get('concurrency', {}))
    rate = limits['requests_per_minute'] / 60
    capacity = limits['burst']
    state = data_root() / '.web-state'
    protect_path(state)
    state.mkdir(parents=True, exist_ok=True)
    target = state / 'ai-rate.json'
    lock_path = state / 'ai-rate.lock'
    protect_path(target); protect_path(lock_path)
    with lock_path.open('a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            diag.event('ai.deferred', reason='rate_lock_busy')
            raise ResourceDeferred(time.time() + 1, 'AI 服务资源繁忙，请稍后重试') from None
        now = time.time()
        previous = read_json(target) if target.exists() else {'tokens': capacity, 'updated_at': now}
        tokens, updated = previous['tokens'], previous['updated_at']
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
               for v in (tokens, updated)) or tokens < 0:
            raise ValueError('AI 请求预算记录无效，请检查资源状态')
        clock = max(now, updated)  # A backwards wall clock never grants extra tokens.
        tokens = min(capacity, tokens + max(0, now - updated) * rate)
        allowed = tokens >= 1 - 1e-9
        write_json_atomic(target, {'version': 1, 'tokens': max(0, tokens - 1) if allowed else tokens,
                                   'updated_at': clock})
        if not allowed:
            diag.event('ai.deferred', reason='rate_budget', not_before=clock + (1-tokens)/rate)
            raise ResourceDeferred(clock + (1 - tokens) / rate, 'AI 请求预算已用完，请稍后重试')
