"""Development download adapter: existing OSS objects only, no remote writes."""
import importlib.util
import os
import sys
from pathlib import Path

from .runtime import environment, require_material_fetch
from .source_cache import check_drama, check_episode, hub_script
from .util import load_env_file


def main():
    if environment() != 'development':
        raise RuntimeError('仅用于本地开发下载')
    require_material_fetch()
    drama, episode, destination = sys.argv[1:]
    check_drama(drama)
    check_episode(episode)
    load_env_file(Path(os.environ['EXACTCUT_DEV_FETCH_ENV']))
    spec = importlib.util.spec_from_file_location('dev_material_hub', hub_script())
    hub = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hub)
    store = hub.OssStore()
    key = f'assets/{drama}/video/{episode}.mp4'
    if not store.exists(key):
        raise RuntimeError(f'{drama}/{episode} 正片在素材库中不存在；本地下载不会触发远端同步')
    store.download(key, Path(destination))


if __name__ == '__main__':
    main()
