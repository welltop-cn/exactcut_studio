"""Optional lane limits; legacy jobs retain conservative resource mappings."""
import math
from .util import load_config

LANES = ('fetch', 'render', 'upload', 'poll')


def ai_settings(config):
    values = config.get('ai', {})
    if not isinstance(values, dict):
        raise ValueError('concurrency.ai 必须为对象')
    rate = values.get('requests_per_minute', 6)
    burst = values.get('burst', 1)
    if isinstance(rate, bool) or not isinstance(rate, (int, float)) or not math.isfinite(rate) or rate <= 0:
        raise ValueError('concurrency.ai.requests_per_minute 必须为有限正数')
    if isinstance(burst, bool) or not isinstance(burst, int) or burst < 1:
        raise ValueError('concurrency.ai.burst 必须为正整数')
    return {'requests_per_minute': rate, 'burst': burst}


def settings():
    config = load_config().get('concurrency', {})
    if not isinstance(config, dict):
        raise ValueError('concurrency 必须为对象')
    enabled = config.get('enabled', True)
    if not isinstance(enabled, bool):
        raise ValueError('concurrency.enabled 必须为布尔值')
    capacities = {}
    for name in LANES:
        value = config.get(name, 1)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ValueError('concurrency.' + name + ' 必须为正整数')
        capacities[name] = value
    total = config.get('max_active_children', sum(capacities.values()))
    if not isinstance(total, int) or isinstance(total, bool) or total < 1:
        raise ValueError('concurrency.max_active_children 必须为正整数')
    previews = config.get('hook_previews', 1)
    if isinstance(previews, bool) or not isinstance(previews, int) or previews < 1:
        raise ValueError('concurrency.hook_previews 必须为正整数')
    return {**capacities, 'max_active_children': total, 'enabled': enabled,
            'hook_previews': previews, 'ai': ai_settings(config)}


def lane(job):
    value = job.get('lane') or job.get('phase') or job.get('action')
    if value in LANES:
        return value
    if value in {'download', 'prefetch', 'refresh_catalog'}:
        return 'fetch'
    if value == 'publish':
        return 'upload'
    return 'render'
