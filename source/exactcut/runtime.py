"""Explicit environment boundaries for code, durable data and external effects."""
import hashlib
import os
from pathlib import Path
from contextlib import contextmanager
import fcntl

CODE_ROOT = Path(__file__).resolve().parent.parent

def data_root():
    root = Path(os.environ.get('EXACTCUT_DATA_ROOT', str(CODE_ROOT))).expanduser().resolve()
    if environment() != 'production' and ((root/'.web-state/production.json').exists() or (root/'.web-state').is_symlink()):
        raise RuntimeError('隔离环境禁止使用生产数据目录')
    return root

def protect_path(path):
    if environment() == 'production':
        return
    target = Path(path).expanduser().resolve()
    for ancestor in [target, *target.parents]:
        if (ancestor/'.web-state/production.json').exists():
            raise RuntimeError('隔离环境禁止使用生产文件或生产密钥')

def environment():
    value = os.environ.get('EXACTCUT_ENVIRONMENT', 'development')
    if value not in {'production', 'development', 'staging', 'test'}:
        raise RuntimeError('Invalid EXACTCUT_ENVIRONMENT')
    return value

def instance_marker():
    return '--instance=' + hashlib.sha256(str(data_root()).encode()).hexdigest()[:20]

def require_external(operation):
    if environment() != 'production' or os.environ.get('EXACTCUT_ALLOW_EXTERNAL_WRITES') != '1':
        raise RuntimeError(f'{operation} 已禁用：隔离环境不能调用生产外部服务')

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
