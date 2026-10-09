from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from exactcut.runtime import require_external
require_external('修改生产 Material Hub')
p=Path(__file__).resolve().parents[2]/'integrations/drama-material-hub/scripts/material_hub.py'
s=p.read_text()
if '--episode-limit' in s:
    print('Episode limit support already installed')
    raise SystemExit(0)
s=s.replace('    started = time.monotonic()\n    drama_ids, skipped_ids = parse_drama_id_selection(', '''    global MAX_PUBLIC_EPISODES
    limit = getattr(args, "episode_limit", 10)
    if not 1 <= limit <= 500:
        raise HubError("episode-limit 必须为 1～500")
    MAX_PUBLIC_EPISODES = limit
    started = time.monotonic()
    drama_ids, skipped_ids = parse_drama_id_selection(''',1)
s=s.replace('    sync_parser.add_argument("--only", help="仅同步指定类别")', '    sync_parser.add_argument("--episode-limit", type=int, default=10, help="本次正片获取上限，默认10，剪辑引用后续集时可扩展，最大500")\n    sync_parser.add_argument("--only", help="仅同步指定类别")')
p.write_text(s)
