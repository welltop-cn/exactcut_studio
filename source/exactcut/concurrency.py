"""Optional lane limits; legacy jobs retain conservative resource mappings."""
import math
from .util import load_config
from .util import read_json

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
    section = load_config().get('concurrency')
    if section is None:section={}
    if not isinstance(section, dict):
        raise ValueError('concurrency 必须为对象')
    enabled = section.get('enabled', True)
    if not isinstance(enabled, bool):
        raise ValueError('concurrency.enabled 必须为布尔值')
    capacities = {}
    for name in LANES:
        value = section.get(name, 1)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ValueError('concurrency.' + name + ' 必须为正整数')
        capacities[name] = value
    # A configured section opts into per-lane concurrency. Default the global
    # ceiling to the sum of lanes so it never couples or starves lanes; only an
    # explicit lower value (emergency lever) reduces it.
    total = section.get('max_active_children', sum(capacities.values()))
    if not isinstance(total, int) or isinstance(total, bool) or total < 1:
        raise ValueError('concurrency.max_active_children 必须为正整数')
    # Legacy hook_previews was a submission-time gate. Hook work now shares the
    # persistent interactive preview workers and stage resources.
    return {**capacities, 'max_active_children': total, 'enabled': enabled,
            'ai': ai_settings(section)}


def scope(job, batch_id=None):
    return (batch_id if batch_id is not None else job.get('batch_id'), job.get('request_id'))


def scope_blocked(candidate, residents):
    batch, request = candidate
    return any(other_batch == batch and (request is None or other_request is None or request == other_request)
               for other_batch, other_request in residents)


def lane(job):
    value = job.get('lane') or job.get('phase') or job.get('action')
    if value in LANES:
        return value
    if value in {'download', 'prefetch', 'refresh_catalog'}:
        return 'fetch'
    if value == 'publish':
        return 'upload'
    return 'render'


def owner_round_robin(entries, owner_for, cursor_file):
    """Keep input FIFO within owners, starting after the last admitted owner."""
    groups = {}
    for entry in entries:
        groups.setdefault(owner_for(entry), []).append(entry)
    owners = list(groups)
    cursor = read_json(cursor_file).get('owner') if cursor_file.exists() else None
    if cursor in owners:
        offset = owners.index(cursor) + 1
        owners = owners[offset:] + owners[:offset]
    while any(groups.values()):
        for owner in owners:
            if groups[owner]:
                yield owner, groups[owner].pop(0)
