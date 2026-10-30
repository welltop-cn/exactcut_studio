"""Selective server-side Gemini credential reading; never loads unrelated values."""
from pathlib import Path
from .runtime import protect_path


def read_key(path):
    path = Path(path)
    if not path.is_absolute():
        raise ValueError('请指定本机 Gemini 凭据文件的绝对路径')
    protect_path(path)
    try:
        for raw in path.read_text(encoding='utf-8-sig').splitlines():
            key, sep, value = raw.strip().removeprefix('export ').partition('=')
            if sep and key.strip() == 'GEMINI_API_KEY' and value.strip().strip("'\""):
                return value.strip().strip("'\"")
    except OSError:
        pass
    raise ValueError('本机 Gemini 凭据不可用，请检查开发启动配置')
