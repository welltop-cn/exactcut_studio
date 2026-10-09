"""Drama catalog stored at .web-state/catalog.json. This process does not query an external source."""
from __future__ import annotations
import json
import re

from .runtime import data_root, require_material_fetch

ROOT = data_root()
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
    # The design gates this on require_material_fetch and names no catalog command.
    # The only external contract is drama_id, episode, dest_path for episode files.
    require_material_fetch()
    raise RuntimeError('剧目清单不在本进程查询外部数据源，请直接维护 catalog.json')


if __name__ == '__main__':
    refresh()
