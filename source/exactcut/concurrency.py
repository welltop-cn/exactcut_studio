"""Optional lane limits; legacy jobs retain conservative resource mappings."""
from .util import load_config

LANES = ('fetch', 'render', 'upload', 'poll')


def settings():
    config = load_config().get('concurrency', {})
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
    return {**capacities, 'max_active_children': total, 'enabled': enabled}


def lane(job):
    value = job.get('lane') or job.get('phase') or job.get('action')
    if value in LANES:
        return value
    if value in {'download', 'prefetch', 'refresh_catalog'}:
        return 'fetch'
    if value == 'publish':
        return 'upload'
    return 'render'
