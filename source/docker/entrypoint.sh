#!/bin/sh
set -eu
python - <<'PY'
import secrets
from exactcut.runtime import project_env_file, claim_data_root, source_root
from exactcut.util import load_env_file
from exactcut.storage import policy
from exactcut.webserver import web_base_path
config = project_env_file()
if not config.is_file():
    raise RuntimeError('启动配置文件不存在')
load_env_file(config)
policy()
web_base_path()
root = claim_data_root()
source_root()
for name in ('projects', 'feedback', 'sources'):
    (root / name).mkdir(parents=True, exist_ok=True)
path = root / '.web-state/password'
if not path.exists() or not path.stat().st_size:
    path.write_text(secrets.token_urlsafe(32))
path.chmod(0o600)
PY
exec python -m exactcut.webserver
