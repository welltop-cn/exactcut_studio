#!/usr/bin/env python3
"""Import a Feishu Wiki online document as the script for a drama.

Usage:
  python import_wiki_script.py <drama_id> <wiki_url>
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from material_hub import (  # noqa: E402
    AUTO_CUT_ROOT,
    DOC_EXTENSIONS,
    FeishuClient,
    OssStore,
    HubError,
)

DRAMA_ID = sys.argv[1]
WIKI_URL = sys.argv[2]


def main() -> None:
    feishu = FeishuClient()
    oss = OssStore()

    with tempfile.TemporaryDirectory(prefix="drama-wiki-script-") as tmp:
        tmp_path = Path(tmp)
        raw = tmp_path / "script_raw.docx"
        feishu.download_online_document(WIKI_URL, raw)

        name = f"剧本_{DRAMA_ID}.docx"
        # Prefer the document title if available.
        try:
            from material_hub import parse_docx
            first_lines = parse_docx(raw).strip().splitlines()
            title = (first_lines[0].strip() if first_lines else "")[:60]
            if title:
                clean = "".join(c for c in title if c not in '\\/:*?"<>|').strip()
                if clean:
                    name = f"{clean}_{DRAMA_ID}.docx"
        except Exception:
            pass

        key = f"assets/{DRAMA_ID}/script/{name}"
        oss.upload_file(key, raw)
        print(f"OSS 上传完成: {key}")

        local = AUTO_CUT_ROOT / f"#{DRAMA_ID}#" / "source" / "docx" / name
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(raw.read_bytes())
        print(f"本地镜像完成: {local}")


if __name__ == "__main__":
    main()
