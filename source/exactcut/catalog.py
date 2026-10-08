"""Persistent drama catalog; source queries reuse Material Hub's client."""
from __future__ import annotations
import importlib.util
import json
import re
import time
from pathlib import Path

from .runtime import CODE_ROOT, data_root, require_external, instance_marker, environment, release_id, maintenance_file

ROOT = data_root()
HUB_SCRIPT = Path.home() / '.openclaw/agents/main/agent/workshop-skills/drama-material-hub/scripts/material_hub.py'
HUB_PYTHON = Path.home() / '.openclaw/workspace/skills/oss-manager/.venv/bin/python'
CATALOG = ROOT / '.web-state/catalog.json'


def merge_catalog(series, supply, attribution=()):
    entries = {}
    for source, rows in [('attribution', attribution), ('supply', supply), ('series', series)]:
        for row in rows:
            drama_id = str(row.get('id') or '').strip()
            title = re.sub(r'^[（(]头[）)]\s*', '', str(row.get('title') or '').strip())
            if re.fullmatch(r'\d{6}', drama_id) and 101400 <= int(drama_id) <= 999999 and title:
                entries[drama_id] = {'id': drama_id, 'title': title, 'source': source}
    return sorted(entries.values(), key=lambda row: row['id'])


def load_catalog():
    if not CATALOG.exists():
        return {'updated': None, 'dramas': []}
    return json.loads(CATALOG.read_text())


def refresh():
    require_external('物料中心查询')
    spec = importlib.util.spec_from_file_location('material_hub', HUB_SCRIPT)
    hub = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hub)
    client = hub.MetabaseClient()
    series = client.query('SELECT id, title FROM toc_series WHERE id >= 101400 AND id <= 999999 ORDER BY id')
    supply = client.query("SELECT drama_id AS id, title FROM supply_tasks WHERE drama_id >= '101400' AND drama_id <= '999999' ORDER BY created_at")
    attribution = client.query('SELECT DISTINCT watch_series_id AS id, watch_series_title AS title FROM dmd_store_order_attribution WHERE watch_series_id >= 101400 AND watch_series_id <= 999999')
    payload = {'updated': time.time(), 'dramas': merge_catalog(series, supply, attribution)}
    if not payload['dramas']:
        raise RuntimeError('数据源未返回剧目，保留现有候选')
    CATALOG.parent.mkdir(exist_ok=True)
    temp = CATALOG.with_suffix('.tmp')
    temp.write_text(json.dumps(payload, ensure_ascii=False))
    temp.replace(CATALOG)
    print(json.dumps({'status': 'updated', 'count': len(payload['dramas']), 'first': payload['dramas'][0]['id'], 'last': payload['dramas'][-1]['id']}, ensure_ascii=False))


if __name__ == '__main__':
    refresh()
