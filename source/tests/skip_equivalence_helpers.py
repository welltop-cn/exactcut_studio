"""跳过优化的业务快照工具；忽略项仅限时间字段及非业务文件。"""
from dataclasses import dataclass
import hashlib
import json

from exactcut import webserver as web, workflow
from exactcut.util import read_json


# 时间值允许跨运行不同；字段本身的新增、删除以及其他业务字段仍参与比较。
TIME_FIELDS = frozenset({
    'updated_at', 'created_at', 'created_at_epoch', 'created', 'finished', 'started',
    'heartbeat_at', 'not_before', 'approved_at', 'last_progress_at',
    'execution_started_at', 'execution_ended_at', 'stage_started_at', 'at',
    'committed_at', 'deleted_at',
})


def normalize(value):
    if isinstance(value, dict):
        return {key: '<时间>' if key in TIME_FIELDS and item is not None else normalize(item)
                for key, item in value.items()}
    if isinstance(value, list):
        return [normalize(item) for item in value]
    return value


def field_diff(before, after, path='$'):
    """给出含文件名、字段路径和前后值的可读差异，包括新增和删除。"""
    if type(before) is not type(after):
        return [f'{path}: {before!r} -> {after!r}']
    if isinstance(before, dict):
        lines = []
        for key in sorted(before.keys() | after.keys()):
            child = f'{path}.{key}'
            if key not in before:
                lines.append(f'{child}: 新增 {after[key]!r}')
            elif key not in after:
                lines.append(f'{child}: 删除 {before[key]!r}')
            else:
                lines.extend(field_diff(before[key], after[key], child))
        return lines
    if isinstance(before, (list, tuple)):
        if len(before) != len(after):
            return [f'{path}: 列表长度 {len(before)} -> {len(after)}']
        return [line for index, (a, b) in enumerate(zip(before, after))
                for line in field_diff(a, b, f'{path}[{index}]')]
    return [] if before == after else [f'{path}: {before!r} -> {after!r}']


@dataclass
class StateDigest:
    digest: str
    files: dict

    def diff(self, other, *, excluding=()):
        excluded = set(excluding)
        return field_diff({k: v for k, v in self.files.items() if k not in excluded},
                          {k: v for k, v in other.files.items() if k not in excluded})


def state_digest(root):
    files = {}
    state_root = root / '.web-state'
    for path in sorted(state_root.rglob('*.json')):
        relative = path.relative_to(state_root)
        if (any(part in {'media-locks', 'logs'} or part.startswith('reservations')
                or part.endswith('.lock') for part in relative.parts)):
            continue
        files[relative.as_posix()] = normalize(read_json(path))
    encoded = json.dumps(files, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return StateDigest(hashlib.sha256(encoded.encode()).hexdigest(), files)


def assert_state_unchanged(before, after, *, excluding=()):
    differences = before.diff(after, excluding=excluding)
    assert not differences, '\n'.join(differences)


def api_snapshot(handler, accounts):
    """进程内复现 sidebar 和详情路由，沿用真实 Handler 权限，无 HTTP/网络。"""
    result = {}
    for account in accounts:
        handler.user = account
        sidebar = workflow.summaries(handler.can_access, compact=True)
        details = {}
        for path in sorted(workflow.BATCHES.iterdir()):
            if not (path / 'draft.json').exists():
                continue
            details[path.name] = ((200, web.batch_response(path.name)) if handler.can_access(path.name)
                                  else (404, {'error': '项目不存在或无权访问'}))
        result[account['username']] = {
            'sidebar': {'batches': [{**batch, 'owner': web.accounts.owner(workflow.BATCHES / batch['id'])}
                                    for batch in sidebar]},
            'details': details,
        }
    return result


def assert_api_equal(before, after):
    # 接口比较不归一化时间，必须逐字段完全相同。
    differences = field_diff(before, after)
    assert not differences, '\n'.join(differences)
