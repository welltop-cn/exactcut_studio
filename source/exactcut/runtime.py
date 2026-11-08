"""Explicit environment boundaries for code, durable data and external effects."""
import hashlib
import json
import os
from pathlib import Path
from contextlib import contextmanager
import fcntl

CODE_ROOT = Path(__file__).resolve().parent.parent

def data_root():
    root = Path(os.environ.get('EXACTCUT_DATA_ROOT', str(CODE_ROOT))).expanduser().resolve()
    if environment() != 'production' and ((root/'.web-state/production.json').exists() or (root/'.web-state').is_symlink()):
        raise RuntimeError('隔离环境禁止使用生产数据目录')
    _check_data_environment(root)
    return root

def _check_data_environment(root):
    marker = root / '.web-state/environment.json'
    if marker.exists():
        owner = json.loads(marker.read_text())['environment']
        if owner != environment():
            raise RuntimeError(f'数据目录属于 {owner} 环境，当前环境为 {environment()}')

def claim_data_root():
    """Persist environment ownership before initializing accounts or jobs."""
    from .util import write_json_atomic
    root = data_root()
    state = root / '.web-state'
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (state / 'environment.lock').open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        _check_data_environment(root)
        write_json_atomic(state / 'environment.json', {'environment': environment()})
    return root

def project_env_file():
    raw = os.environ.get('EXACTCUT_ENV_FILE', '').strip()
    return Path(raw).expanduser() if raw else data_root() / '.env'

def validate_env_owner(path, values):
    owner = values.get('EXACTCUT_ENVIRONMENT')
    filename_owner = next((value for value in ('development', 'staging', 'production', 'test')
                           if Path(path).name in {f'.env.{value}', f'{value}.env'}), None)
    if ((owner and owner != environment()) or (filename_owner and filename_owner != environment())):
        raise RuntimeError('配置环境与当前进程环境不一致；拒绝读取配置')

def source_root():
    """Absolute episode cache. Unset in dev/test means <data root>/sources."""
    raw = os.environ.get('EXACTCUT_SOURCE_ROOT', '').strip()
    if raw:
        root = Path(raw).expanduser()
        if not root.is_absolute():
            raise RuntimeError('EXACTCUT_SOURCE_ROOT 必须是绝对路径')
        root = root.resolve()
    else:
        root = (data_root() / 'sources').resolve()
    text = root.as_posix()
    if root == Path.home().resolve() or '.openclaw' in text or 'Downloads/auto-cut' in text:
        raise RuntimeError('正片缓存不能放在家目录、OpenClaw 或下载目录')
    protect_path(root)
    return root

def protect_path(path):
    target = Path(path).expanduser().resolve()
    for ancestor in [target, *target.parents]:
        _check_data_environment(ancestor)
        if environment() != 'production' and (ancestor/'.web-state/production.json').exists():
            raise RuntimeError('隔离环境禁止使用生产文件或生产密钥')

def environment():
    value = os.environ.get('EXACTCUT_ENVIRONMENT', 'development')
    if value not in {'production', 'development', 'staging', 'test'}:
        raise RuntimeError('Invalid EXACTCUT_ENVIRONMENT')
    return value

def instance_marker():
    from . import tick_snapshot
    # data_root() derives its path from EXACTCUT_DATA_ROOT, expanduser/resolve;
    # its remaining reads validate environment ownership. Worker ticks do not
    # change that environment or redirect the data root. Cache only this tick:
    # CLI/HTTP/tests outside the scope must still observe environment changes.
    return tick_snapshot.instance_marker(
        lambda: '--instance=' + hashlib.sha256(str(data_root()).encode()).hexdigest()[:20])

def require_external(operation):
    if environment() != 'production' or os.environ.get('EXACTCUT_ALLOW_EXTERNAL_WRITES') != '1':
        raise RuntimeError(f'{operation} 已禁用：隔离环境不能调用生产外部服务')

def _require_flag(flag):
    if environment() != 'production' or os.environ.get(flag) != '1':
        raise RuntimeError(f'已禁用：需要 {flag}=1')

def require_publish():
    _require_flag('EXACTCUT_ALLOW_PUBLISH')

def require_material_fetch():
    flag = 'EXACTCUT_ALLOW_MATERIAL_FETCH'
    if os.environ.get(flag) != '1' or environment() not in {'production', 'development'}:
        raise RuntimeError(f'已禁用：需要 {flag}=1')

def require_ai():
    flag = 'EXACTCUT_ALLOW_AI'
    if os.environ.get(flag) != '1' or environment() not in {'production', 'development'}:
        raise RuntimeError(f'已禁用：需要 {flag}=1')

def feature_flags():
    return {
        'publish': os.environ.get('EXACTCUT_ALLOW_PUBLISH') == '1',
        'material_fetch': os.environ.get('EXACTCUT_ALLOW_MATERIAL_FETCH') == '1',
        'ai': os.environ.get('EXACTCUT_ALLOW_AI') == '1',
    }

def maintenance_file():
    return data_root() / '.web-state/maintenance.json'

def release_id():
    return os.environ.get('EXACTCUT_RELEASE_ID', 'unversioned')

@contextmanager
def mutation_lock():
    path = data_root() / '.web-state/mutation.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield
