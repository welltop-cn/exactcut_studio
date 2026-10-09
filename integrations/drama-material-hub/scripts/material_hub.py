#!/usr/bin/env python3
"""Synchronize complete drama source materials to OSS and local auto-cut."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse
from xml.etree import ElementTree
from zipfile import ZipFile

import requests

try:
    import oss2
except ImportError as exc:
    raise SystemExit(
        "Missing oss2. Run this script with the project virtualenv."
    ) from exc


def project_env_file() -> Path:
    override = os.environ.get("EXACTCUT_ENV_FILE", "").strip()
    if override:
        return Path(override).expanduser()
    if os.environ.get("EXACTCUT_ENVIRONMENT", "development") != "development":
        raise RuntimeError("此环境需要明确指定 EXACTCUT_ENV_FILE，禁止回退本机配置")
    return Path(__file__).resolve().parents[3] / "source" / ".env.development"


def load_project_env() -> None:
    path = project_env_file()
    if not path.is_file():
        return
    values = {}
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        values[key.strip()] = value.strip().strip("'\"")
    current = os.environ.get("EXACTCUT_ENVIRONMENT", "development")
    filename_owner = next((value for value in ("development", "staging", "production", "test")
                           if path.name in {f".env.{value}", f"{value}.env"}), None)
    if ((values.get("EXACTCUT_ENVIRONMENT") and values["EXACTCUT_ENVIRONMENT"] != current)
            or (filename_owner and filename_owner != current)):
        raise RuntimeError("配置环境与当前进程环境不一致；拒绝读取配置")
    for key, value in values.items():
        os.environ.setdefault(key, value)


load_project_env()

HOME = Path.home()
SKILL_DIR = Path(__file__).resolve().parent.parent
STATE_FILE = SKILL_DIR / "data" / "sync_state.json"
SOURCE_LEDGER_FILE = SKILL_DIR / "data" / "source_sync_ledger.json"
SOURCE_LEDGER_LOCK_FILE = SKILL_DIR / "data" / "source_sync_ledger.lock"


def material_local_root() -> Path:
    raw = os.environ.get("MATERIAL_HUB_LOCAL_ROOT", "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path(__file__).resolve().parents[3] / "source" / "material-local"


AUTO_CUT_ROOT = material_local_root()
QUALITY_SYNC_SCRIPT = SKILL_DIR / "scripts" / "quality_sync.py"
SUGGESTION_SOURCE_FILE = SKILL_DIR / "data" / "suggestion_sources.json"

TARGET_BUCKET_DEFAULT = "aigc-us02-content"
SOURCE_BUCKET_DEFAULT = "aigc-us02"
SOURCE_PREFIX = "tb/tbl5wK7ykMP9ZNKb"
TARGET_PREFIX = "assets"
MAX_PUBLIC_EPISODES = 10
FEISHU_NOTIFY_CHAT_ID = "oc_b13decd7cb7b8f1a62341499584218f4"
FEISHU_NOTIFY_CHAT_NAME = "🤖物料D"
MAX_CARD_DRAMAS = 12
SUPPORTED_DRAMA_ID_DIGITS = 6
MIN_SUPPORTED_DRAMA_ID = 101050
MAX_SUPPORTED_DRAMA_ID = 999999
EXCLUDED_DRAMA_ID_REASON = "仅处理6位且在101050～999999范围内的剧ID"
# Explicitly opt-in, one-off support for two legacy projects. These IDs stay
# outside the normal/Cron range and are never enabled implicitly.
TEMPORARY_LEGACY_DRAMA_IDS = frozenset({"101018", "101026"})
SOURCE_BASELINE_COMPLETED_THROUGH = 101426
SOURCE_ALREADY_COMPLETED_REASON = "已在source同步记录表中，跳过重复下载和上传"

CATEGORIES = ("metadata", "videos", "script", "suggestions", "quality")
CATEGORY_ALIASES = {
    "metadata": "metadata",
    "meta": "metadata",
    "info": "metadata",
    "drama_info": "metadata",
    "drama-info": "metadata",
    "信息": "metadata",
    "元信息": "metadata",
    "videos": "videos",
    "video": "videos",
    "episodes": "videos",
    "正片": "videos",
    "视频": "videos",
    "script": "script",
    "scripts": "script",
    "剧本": "script",
    "suggestions": "suggestions",
    "suggestion": "suggestions",
    "素材建议": "suggestions",
    "建议": "suggestions",
    "quality": "quality",
    "quality-materials": "quality",
    "优质素材": "quality",
    "好素材": "quality",
}

FEISHU_PARENTS = (
    ("内部制作", "SUqpf2x8Wl7UiCdiYlkcVtmmnMb"),
    ("供应商制作", "ZGEXfWHqql8FKQdSD6HcFDsNnDc"),
)

DOC_EXTENSIONS = {".doc", ".docx", ".txt"}
VIDEO_RE = re.compile(r"EP\s*0*(\d+)", re.IGNORECASE)
WORD_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


class HubError(RuntimeError):
    """Fatal integration error."""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        raise HubError(f"配置文件不存在: {path}")
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'").strip('"')
    return values


def normalize_drama_id(value: str | int) -> str:
    cleaned = str(value).strip().strip("#").strip()
    if not re.fullmatch(r"\d{4,12}", cleaned):
        raise argparse.ArgumentTypeError(f"无效剧ID: {value}")
    return cleaned


def is_supported_drama_id(drama_id: str) -> bool:
    return (
        len(drama_id) == SUPPORTED_DRAMA_ID_DIGITS
        and MIN_SUPPORTED_DRAMA_ID <= int(drama_id) <= MAX_SUPPORTED_DRAMA_ID
    )


def parse_drama_id_selection(
    values: Iterable[str],
    *,
    allow_legacy: bool = False,
) -> tuple[list[str], list[str]]:
    included: list[str] = []
    excluded: list[str] = []
    for value in values:
        for part in re.split(r"[\s,，]+", value.strip()):
            if not part:
                continue
            drama_id = normalize_drama_id(part)
            legacy_allowed = allow_legacy and drama_id in TEMPORARY_LEGACY_DRAMA_IDS
            target = included if is_supported_drama_id(drama_id) or legacy_allowed else excluded
            if drama_id not in target:
                target.append(drama_id)
    if not included and not excluded:
        raise argparse.ArgumentTypeError("至少提供一个剧ID")
    return included, excluded


def parse_categories(value: str | None) -> set[str]:
    if not value:
        return set(CATEGORIES)
    result: set[str] = set()
    for raw in re.split(r"[\s,，]+", value.strip()):
        if not raw:
            continue
        category = CATEGORY_ALIASES.get(raw.lower()) or CATEGORY_ALIASES.get(raw)
        if not category:
            raise argparse.ArgumentTypeError(f"未知物料类别: {raw}")
        result.add(category)
    return result


def sql_quote(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "''")


def safe_filename(name: str, fallback: str) -> str:
    value = Path(name or fallback).name.replace("\x00", "").strip()
    return value or fallback


def normalize_match_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value or "").lower()
    normalized = re.sub(r"^\s*#?\d{4,12}#?[\s_\-—]*", "", normalized)
    return re.sub(r"[\s_\-—:：'\"“”‘’.,，。!?！？()\[\]{}]+", "", normalized)


def clean_suggestion_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\u00a0", " ")
    lines: list[str] = []
    blank = False
    for raw in text.split("\n"):
        line = raw.replace("\t", "    ").strip()
        line = re.sub(r"[ \u3000]+$", "", line)
        if not line:
            if not blank and lines:
                lines.append("")
            blank = True
            continue
        blank = False
        lines.append(line)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines).strip() + ("\n" if lines else "")


def suggestion_source_url(drama_id: str) -> str | None:
    """Return an explicitly registered Feishu Wiki source for suggestions."""
    try:
        payload = json.loads(SUGGESTION_SOURCE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    value = payload.get(str(drama_id)) if isinstance(payload, dict) else None
    return str(value).strip() if value else None


def parse_docx(path: Path) -> str:
    with ZipFile(path) as archive:
        xml_data = archive.read("word/document.xml")
    root = ElementTree.fromstring(xml_data)
    paragraphs: list[str] = []
    for paragraph in root.iter(WORD_NS + "p"):
        texts = [node.text for node in paragraph.iter(WORD_NS + "t") if node.text]
        paragraphs.append("".join(texts))
    return "\n".join(paragraphs)


def parse_text_file(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".docx":
        return parse_docx(path)
    if suffix == ".doc":
        try:
            result = subprocess.run(
                ["antiword", "-m", "UTF-8.txt", str(path)],
                capture_output=True,
                encoding="utf-8",
                timeout=60,
            )
        except FileNotFoundError as exc:
            raise HubError("读取旧版 DOC 附件需要安装 antiword；正片下载不需要该工具") from exc
        except (OSError, UnicodeError, subprocess.TimeoutExpired) as exc:
            raise HubError(f"DOC 解析失败: {exc}") from exc
        if result.returncode != 0:
            raise HubError(f"DOC 解析失败: {result.stderr.strip()}")
        return result.stdout
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    raise HubError(f"无法识别文本编码: {path.name}")


def extract_mp4_url(value: Any) -> str | None:
    if not value:
        return None
    data = value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith(("http://", "https://")):
            return stripped
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError:
            match = re.search(r"https?://[^\s\"']+", stripped)
            return match.group(0) if match else None
    if isinstance(data, dict):
        for key in ("link", "url", "src", "mp4"):
            found = extract_mp4_url(data.get(key))
            if found:
                return found
    if isinstance(data, list):
        for item in data:
            found = extract_mp4_url(item)
            if found:
                return found
    return None


class MetabaseClient:
    def __init__(self) -> None:
        self.host = os.environ.get("METABASE_HOST", "").rstrip("/")
        self.user = os.environ.get("METABASE_USER", "")
        self.password = os.environ.get("METABASE_PASSWORD", "")
        if not all((self.host, self.user, self.password)):
            raise HubError("Metabase 配置缺失")
        self.session = requests.Session()
        # Metabase is an HTTP endpoint reachable directly from this host.  The
        # local HTTPS proxy returns 502 for this host and can leave the TLS
        # handshake hanging, so do not inherit proxy environment variables for
        # metadata/source discovery requests.
        self.session.trust_env = False
        response = self.session.post(
            f"{self.host}/api/session",
            json={"username": self.user, "password": self.password},
            timeout=20,
        )
        response.raise_for_status()
        self.token = response.json().get("id")
        if not self.token:
            raise HubError("Metabase 登录失败")

    def query(self, sql: str) -> list[dict[str, Any]]:
        response = self.session.post(
            f"{self.host}/api/dataset",
            headers={"X-Metabase-Session": self.token},
            json={"database": 2, "type": "native", "native": {"query": sql}},
            timeout=60,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("error"):
            raise HubError(f"Metabase 查询失败: {payload['error']}")
        data = payload.get("data", {})
        columns = [item.get("name") for item in data.get("cols", [])]
        return [dict(zip(columns, row)) for row in data.get("rows", [])]

    def series_info(self, drama_id: str) -> dict[str, Any]:
        rows = self.query(
            f"""
SELECT id, title, episode_count, publish_count, visible_count,
       is_completion, copyright, first_pay_episode, ad_unlock_start_episode,
       last_publish_at, next_publish_at, create_at
FROM toc_series
WHERE id = {int(drama_id)}
LIMIT 1
"""
        )
        if rows:
            return rows[0]
        fallback = self.query(
            f"""
SELECT DISTINCT watch_series_title
FROM dmd_store_order_attribution
WHERE watch_series_id = {int(drama_id)}
LIMIT 1
"""
        )
        title = fallback[0].get("watch_series_title") if fallback else None
        if title:
            return {"id": int(drama_id), "title": title}
        supply = self.query(
            f"""
SELECT title, total_episodes, created_at, deliverable_url
FROM supply_tasks
WHERE drama_id = '{sql_quote(drama_id)}'
ORDER BY (unlock_point IS NOT NULL) DESC, created_at ASC
LIMIT 1
"""
        )
        if not supply:
            return {}
        row = supply[0]
        supply_title = re.sub(
            r"^[（(]头[）)]\s*",
            "",
            str(row.get("title") or "").strip(),
        )
        return {
            "id": int(drama_id),
            "title": supply_title or None,
            "episode_count": row.get("total_episodes"),
            "create_at": row.get("created_at"),
            "_deliverable_url": row.get("deliverable_url"),
        }

    def public_episodes(self, drama_id: str) -> list[dict[str, Any]]:
        rows = self.query(
            f"""
SELECT trailer_ep, mp4, publish_at, order_num
FROM toc_episode
WHERE series_id = {int(drama_id)}
  AND deleted = 0
  AND hidden = 0
  AND publish_at <= NOW()
ORDER BY order_num ASC
LIMIT {MAX_PUBLIC_EPISODES}
"""
        )
        episodes: list[dict[str, Any]] = []
        for index, row in enumerate(rows, start=1):
            raw_label = str(row.get("trailer_ep") or f"EP{index}")
            match = VIDEO_RE.search(raw_label)
            number = int(match.group(1)) if match else index
            episodes.append(
                {
                    "label": f"EP{number}",
                    "number": number,
                    "url": extract_mp4_url(row.get("mp4")),
                    "publish_at": row.get("publish_at"),
                    "order_num": row.get("order_num"),
                }
            )
        return episodes

    def script_info(self, drama_id: str, title: str | None) -> dict[str, Any]:
        rows = self.query(
            f"""
SELECT sc.file_name, sc.file_url, sc.title
FROM supply_tasks t
LEFT JOIN supply_scripts sc ON t.script_id = sc.id
WHERE t.drama_id = '{sql_quote(drama_id)}'
  AND sc.file_url IS NOT NULL
ORDER BY t.created_at ASC
LIMIT 1
"""
        )
        if rows:
            return rows[0]
        if not title:
            return {}
        safe_title = sql_quote(title[:40])
        rows = self.query(
            f"""
SELECT file_name, file_url, title
FROM supply_scripts
WHERE title LIKE '%{safe_title}%'
   OR extension_name LIKE '%{safe_title}%'
ORDER BY id DESC
LIMIT 1
"""
        )
        return rows[0] if rows else {}

    def discover_new(self, cutover_at: str) -> list[dict[str, Any]]:
        safe_cutover = sql_quote(cutover_at.replace("T", " ").replace("+00:00", ""))
        return self.query(
            f"""
SELECT series_id AS drama_id, MIN(publish_at) AS first_public_at
FROM toc_episode
WHERE deleted = 0
  AND hidden = 0
  AND publish_at <= NOW()
GROUP BY series_id
HAVING MIN(publish_at) >= '{safe_cutover}'
ORDER BY first_public_at ASC
"""
        )


class OssStore:
    def __init__(self) -> None:
        key_id = os.environ.get("ALIYUN_OSS_ACCESS_KEY_ID", "")
        key_secret = os.environ.get("ALIYUN_OSS_ACCESS_KEY_SECRET", "")
        endpoint = os.environ.get("ALIYUN_OSS_ENDPOINT", "")
        if not all((key_id, key_secret, endpoint)):
            raise HubError("OSS 配置缺失")
        auth = oss2.Auth(key_id, key_secret)
        self.target_name = os.environ.get("ALIYUN_OSS_BUCKET") or TARGET_BUCKET_DEFAULT
        self.source_name = os.environ.get("ALIYUN_OSS_SOURCE_BUCKET") or SOURCE_BUCKET_DEFAULT
        self.target = oss2.Bucket(auth, endpoint, self.target_name, connect_timeout=30)
        self.source = oss2.Bucket(auth, endpoint, self.source_name, connect_timeout=30)
        use_local_proxy = os.environ.get("EXACTCUT_ENVIRONMENT") == "development"
        for bucket in (self.target, self.source):
            bucket.session.session.trust_env = use_local_proxy

    def exists(self, key: str, *, source: bool = False) -> bool:
        bucket = self.source if source else self.target
        return bool(bucket.object_exists(key))

    def size(self, key: str, *, source: bool = False) -> int | None:
        bucket = self.source if source else self.target
        try:
            return int(bucket.head_object(key).content_length)
        except Exception:
            return None

    def list(
        self,
        prefix: str,
        *,
        source: bool = False,
    ) -> list[dict[str, Any]]:
        bucket = self.source if source else self.target
        items: list[dict[str, Any]] = []
        for item in oss2.ObjectIterator(bucket, prefix=prefix):
            if item.key.endswith("/"):
                continue
            items.append({"key": item.key, "size": int(item.size)})
        return items

    def read_bytes(self, key: str) -> bytes | None:
        try:
            return self.target.get_object(key).read()
        except Exception:
            return None

    def upload_bytes(self, key: str, data: bytes, content_type: str | None = None) -> None:
        headers = {"Content-Type": content_type} if content_type else None
        result = self.target.put_object(key, data, headers=headers)
        if result.status not in (200, 201):
            raise HubError(f"OSS 上传失败: {key}, status={result.status}")

    def upload_file(self, key: str, path: Path) -> None:
        result = self.target.put_object_from_file(key, str(path))
        if result.status not in (200, 201):
            raise HubError(f"OSS 上传失败: {key}, status={result.status}")

    def copy_source(self, source_key: str, target_key: str) -> bool:
        if not self.exists(source_key, source=True):
            return False
        result = self.target.copy_object(self.source_name, source_key, target_key)
        if result.status not in (200, 201):
            raise HubError(f"OSS 拷贝失败: {source_key} → {target_key}")
        return True

    def download(self, key: str, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        progress = path.with_name(path.name + ".progress.json")

        def report(received: int, total: int) -> None:
            payload = {"received": received, "total": total}
            temporary = progress.with_name(progress.name + ".tmp")
            temporary.write_text(json.dumps(payload), encoding="utf-8")
            temporary.replace(progress)

        progress.unlink(missing_ok=True)
        try:
            for attempt in range(8):
                try:
                    oss2.resumable_download(
                        self.target,
                        key,
                        str(path),
                        multiget_threshold=32 * 1024 * 1024,
                        part_size=2 * 1024 * 1024,
                        num_threads=4,
                        progress_callback=report,
                    )
                    break
                except (requests.exceptions.RequestException, oss2.exceptions.RequestError):
                    if attempt == 7:
                        raise
                    print(f"OSS 下载连接中断，保留已完成分片，重试 {attempt + 1}/7", file=sys.stderr, flush=True)
                    time.sleep(min(2 * (attempt + 1), 10))
        finally:
            progress.unlink(missing_ok=True)


class FeishuClient:
    def __init__(self) -> None:
        app_id = os.environ.get("MATERIAL_HUB_FEISHU_APP_ID", "")
        app_secret = os.environ.get("MATERIAL_HUB_FEISHU_APP_SECRET", "")
        if not app_id or not app_secret:
            raise HubError("飞书应用配置缺失")
        self.session = requests.Session()
        self.session.trust_env = False
        response = self.session.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": app_id, "app_secret": app_secret},
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        self.token = payload.get("tenant_access_token")
        if not self.token:
            raise HubError(f"飞书登录失败: code={payload.get('code')}")
        self.headers = {"Authorization": f"Bearer {self.token}"}

    @staticmethod
    def folder_token_from_url(value: str | None) -> str | None:
        match = re.search(r"/folder/([^/?#]+)", value or "")
        return match.group(1) if match else None

    def list_folder(self, folder_token: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            params: dict[str, Any] = {
                "folder_token": folder_token,
                "page_size": 200,
            }
            if page_token:
                params["page_token"] = page_token
            response = self.session.get(
                "https://open.feishu.cn/open-apis/drive/v1/files",
                headers=self.headers,
                params=params,
                timeout=30,
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("code") != 0:
                raise HubError(f"飞书目录查询失败: code={payload.get('code')}")
            data = payload.get("data", {})
            items.extend(data.get("files", []))
            if not data.get("has_more"):
                return items
            page_token = data.get("page_token")

    def download(self, file_token: str, path: Path) -> None:
        response = self.session.get(
            f"https://open.feishu.cn/open-apis/drive/v1/files/{file_token}/download",
            headers=self.headers,
            allow_redirects=True,
            timeout=(30, 120),
        )
        response.raise_for_status()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(response.content)

    @staticmethod
    def is_online_document_url(url: str) -> bool:
        parsed = urlparse(url or "")
        return bool(
            parsed.hostname
            and parsed.hostname.endswith("feishu.cn")
            and re.fullmatch(r"/(?:docx|wiki)/[^/]+/?", parsed.path)
        )

    def _online_document_token(self, url: str) -> str:
        parsed = urlparse(url)
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) != 2 or parts[0] not in {"docx", "wiki"}:
            raise HubError(f"不支持的飞书在线文档链接: {parsed.path}")
        if parts[0] == "docx":
            return parts[1]

        response = self.session.get(
            "https://open.feishu.cn/open-apis/wiki/v2/spaces/get_node",
            headers=self.headers,
            params={"token": parts[1]},
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        node = payload.get("data", {}).get("node", {})
        if payload.get("code") != 0 or node.get("obj_type") != "docx":
            raise HubError(
                "飞书知识库文档解析失败: "
                f"code={payload.get('code')}, type={node.get('obj_type')}"
            )
        token = node.get("obj_token")
        if not token:
            raise HubError("飞书知识库文档缺少 obj_token")
        return token

    def download_online_document(self, url: str, path: Path) -> None:
        document_token = self._online_document_token(url)
        response = self.session.get(
            f"https://open.feishu.cn/open-apis/docx/v1/documents/"
            f"{document_token}/raw_content",
            headers=self.headers,
            timeout=60,
        )
        response.raise_for_status()
        payload = response.json()
        content = payload.get("data", {}).get("content", "")
        if payload.get("code") != 0 or not content.strip():
            raise HubError(
                f"飞书在线文档读取失败: code={payload.get('code')}"
            )

        # raw_content is plain text; write a minimal WordprocessingML package.
        # This preserves the same plain-text semantics without a system converter.
        path.parent.mkdir(parents=True, exist_ok=True)
        document = ElementTree.Element(WORD_NS + "document")
        body = ElementTree.SubElement(document, WORD_NS + "body")
        for line in content.split("\n"):
            paragraph = ElementTree.SubElement(body, WORD_NS + "p")
            run = ElementTree.SubElement(paragraph, WORD_NS + "r")
            text = ElementTree.SubElement(run, WORD_NS + "t")
            text.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
            text.text = line
        with ZipFile(path, "w") as archive:
            archive.writestr("[Content_Types].xml", (
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                '<Default Extension="xml" ContentType="application/xml"/>'
                '<Override PartName="/word/document.xml" '
                'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
                '</Types>'
            ))
            archive.writestr("_rels/.rels", (
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" '
                'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
                'Target="word/document.xml"/></Relationships>'
            ))
            archive.writestr("word/document.xml", ElementTree.tostring(document, encoding="utf-8", xml_declaration=True))
        try:
            with ZipFile(path) as archive:
                if "word/document.xml" not in archive.namelist():
                    raise HubError("生成的 DOCX 缺少 word/document.xml")
        except Exception as exc:
            path.unlink(missing_ok=True)
            raise HubError(f"飞书在线文档 DOCX 校验失败: {exc}") from exc

    def find_drama_folder(self, drama_id: str, title: str | None) -> str | None:
        wanted = normalize_match_text(title or "")

        def matches(name: str) -> bool:
            if drama_id in name:
                return True
            candidate = normalize_match_text(name)
            return bool(wanted and candidate and (wanted in candidate or candidate in wanted))

        for _label, parent_token in FEISHU_PARENTS:
            try:
                parent_items = self.list_folder(parent_token)
            except Exception:
                continue
            for item in parent_items:
                if item.get("type") != "folder":
                    continue
                if matches(item.get("name", "")):
                    return item.get("token")
                try:
                    children = self.list_folder(item.get("token", ""))
                except Exception:
                    continue
                for child in children:
                    if child.get("type") == "folder" and matches(child.get("name", "")):
                        return child.get("token")
        return None

    def suggestion_file(self, folder_token: str) -> dict[str, Any] | None:
        root = self.list_folder(folder_token)

        def supported(item: dict[str, Any]) -> bool:
            name = item.get("name", "")
            suffix = Path(name).suffix.lower()
            return (
                not name.startswith((".", "~$"))
                and (suffix in DOC_EXTENSIONS or item.get("type") in {"doc", "docx", "txt"})
            )

        for item in root:
            name = item.get("name", "")
            if item.get("type") == "folder" and any(
                key in name for key in ("素材建议", "高光时刻", "高光")
            ):
                candidates = [entry for entry in self.list_folder(item["token"]) if supported(entry)]
                if candidates:
                    return candidates[0]
        candidates = [entry for entry in root if supported(entry)]
        candidates.sort(
            key=lambda item: int(
                "素材建议" in item.get("name", "") or "高光" in item.get("name", "")
            ),
            reverse=True,
        )
        return candidates[0] if candidates else None

    def video_files(self, folder_token: str) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        root = self.list_folder(folder_token)
        collections: list[list[dict[str, Any]]] = []
        for item in root:
            if item.get("type") == "folder" and any(
                key in item.get("name", "") for key in ("投放版", "成片", "视频")
            ):
                collections.append(self.list_folder(item["token"]))
        collections.append(root)
        for entries in collections:
            for item in entries:
                name = item.get("name", "")
                if Path(name).suffix.lower() != ".mp4":
                    continue
                match = VIDEO_RE.search(name)
                if match:
                    result.setdefault(f"EP{int(match.group(1))}", item)
        return result

    def script_file(self, folder_token: str) -> dict[str, Any] | None:
        root = self.list_folder(folder_token)
        collections: list[tuple[int, list[dict[str, Any]]]] = [(0, root)]
        for item in root:
            if item.get("type") != "folder":
                continue
            name = item.get("name", "")
            priority = 2 if any(key in name.lower() for key in ("剧本", "script", "文本")) else 0
            if priority:
                collections.append((priority, self.list_folder(item["token"])))
        candidates: list[tuple[int, dict[str, Any]]] = []
        for priority, entries in collections:
            for item in entries:
                name = item.get("name", "")
                suffix = Path(name).suffix.lower()
                if suffix not in DOC_EXTENSIONS:
                    continue
                if any(key in name for key in ("素材建议", "高光")):
                    continue
                name_priority = 1 if any(
                    key in name.lower() for key in ("剧本", "script")
                ) else 0
                candidates.append((priority + name_priority, item))
        candidates.sort(key=lambda value: value[0], reverse=True)
        return candidates[0][1] if candidates else None

    def send_card(self, chat_id: str, card: dict[str, Any]) -> str | None:
        response = self.session.post(
            "https://open.feishu.cn/open-apis/im/v1/messages",
            headers={**self.headers, "Content-Type": "application/json"},
            params={"receive_id_type": "chat_id"},
            json={
                "receive_id": chat_id,
                "msg_type": "interactive",
                "content": json.dumps(card, ensure_ascii=False),
            },
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != 0:
            raise HubError(
                f"飞书卡片发送失败: code={payload.get('code')}, msg={payload.get('msg')}"
            )
        return payload.get("data", {}).get("message_id")


def _duration_label(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    if seconds < 60:
        return f"{seconds} 秒"
    minutes, remainder = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} 分 {remainder} 秒"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} 小时 {minutes} 分"


def _count_label(category: dict[str, Any], *, expected: bool = False) -> str:
    oss_count = int(category.get("oss_count") or 0)
    local_count = int(category.get("local_count") or 0)
    if expected:
        expected_count = len(category.get("expected") or [])
        return f"OSS {oss_count} · 本地 {local_count} / 应有 {expected_count}"
    return f"OSS {oss_count} · 本地 {local_count}"


def _status_icon(value: bool) -> str:
    return "✅" if value else "⚠️"


def build_result_card(
    results: list[dict[str, Any]],
    *,
    task_label: str,
    elapsed_seconds: float,
    skipped_ids: list[str] | None = None,
    already_completed_ids: list[str] | None = None,
    ledger_recorded_ids: list[str] | None = None,
) -> dict[str, Any]:
    complete = sum(item.get("status") == "complete" for item in results)
    partial = sum(item.get("status") == "partial" for item in results)
    errors = sum(item.get("status") == "error" for item in results)
    action_count = sum(len(item.get("actions") or []) for item in results)

    if errors:
        template = "red"
        title = "❌ 物料同步异常"
        overall = f"{complete} 完整 · {partial} 部分缺失 · {errors} 失败"
    elif partial:
        template = "orange"
        title = "⚠️ 物料同步部分完成"
        overall = f"{complete} 完整 · {partial} 部分缺失"
    else:
        template = "green"
        title = "✅ 物料同步完成"
        overall = f"{complete} 部剧物料完整"

    elements: list[dict[str, Any]] = [
        {
            "tag": "div",
            "fields": [
                {
                    "is_short": True,
                    "text": {
                        "tag": "lark_md",
                        "content": f"**🚀 任务类型**\n{task_label}",
                    },
                },
                {
                    "is_short": True,
                    "text": {
                        "tag": "lark_md",
                        "content": f"**📺 剧集数量**\n{len(results)} 部",
                    },
                },
                {
                    "is_short": True,
                    "text": {
                        "tag": "lark_md",
                        "content": f"**📊 整体结果**\n{overall}",
                    },
                },
                {
                    "is_short": True,
                    "text": {
                        "tag": "lark_md",
                        "content": f"**⏱️ 运行耗时**\n{_duration_label(elapsed_seconds)}",
                    },
                },
            ],
        },
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": (
                    f"本次执行 **{action_count}** 个补齐动作　"
                    f"☁️ OSS → ✂️ auto-cut 已完成一致性检查"
                ),
            },
        },
    ]
    # Keep skip/already-complete IDs in the machine-readable result payload,
    # but omit them from the Feishu card. Large cron batches can contain
    # hundreds of such IDs; they are bookkeeping rather than useful output
    # for the operator and make the card unnecessarily tall.
    if ledger_recorded_ids:
        recorded_label = "、".join(
            f"#{drama_id}#" for drama_id in ledger_recorded_ids
        )
        elements.append(
            {
                "tag": "note",
                "elements": [
                    {
                        "tag": "plain_text",
                        "content": (
                            f"已写入source同步记录表：{recorded_label}"
                        ),
                    }
                ],
            }
        )
    elements.append({"tag": "hr"})

    for index, item in enumerate(results[:MAX_CARD_DRAMAS]):
        status = item.get("status")
        icon = "✅" if status == "complete" else "⚠️" if status == "partial" else "❌"
        drama_id = str(item.get("drama_id") or "-")
        title_text = str(item.get("title") or "未获取剧名").strip()
        if len(title_text) > 72:
            title_text = title_text[:69] + "…"
        categories = item.get("categories") or {}
        metadata = categories.get("metadata") or {}
        videos = categories.get("videos") or {}
        script = categories.get("script") or {}
        suggestions = categories.get("suggestions") or {}
        quality = categories.get("quality") or {}

        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"{icon} **#{drama_id}# · {title_text}**",
                },
            }
        )
        if categories:
            elements.extend(
                [
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": (
                                f"🎬 **正片**　{_status_icon(bool(videos.get('complete')))} "
                                f"{_count_label(videos, expected=True)}\n"
                                f"📄 **剧本**　{_status_icon(bool(script.get('complete')))} "
                                f"{_count_label(script)}"
                            ),
                        },
                    },
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": (
                                f"🧾 **剧信息**　{_status_icon(bool(metadata.get('complete')))}　　"
                                f"💡 **素材建议**　{_status_icon(bool(suggestions.get('complete')))}\n"
                                f"⭐ **优质素材**　{_status_icon(bool(quality.get('complete')))} "
                                f"{_count_label(quality)}"
                            ),
                        },
                    },
                ]
            )

        actions = [str(value) for value in item.get("actions") or []]
        item_errors = [str(value) for value in item.get("errors") or []]
        if actions:
            action_text = "、".join(actions[:4])
            if len(actions) > 4:
                action_text += f" 等 {len(actions)} 项"
            elements.append(
                {
                    "tag": "note",
                    "elements": [
                        {"tag": "plain_text", "content": f"本次动作：{action_text}"}
                    ],
                }
            )
        elif status == "complete":
            elements.append(
                {
                    "tag": "note",
                    "elements": [
                        {
                            "tag": "plain_text",
                            "content": "本次无新增操作，OSS 与本地物料已齐全",
                        }
                    ],
                }
            )
        if item_errors:
            error_text = "；".join(item_errors[:2])
            if len(error_text) > 320:
                error_text = error_text[:317] + "…"
            elements.append(
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"<font color='red'>**异常：** {error_text}</font>",
                    },
                }
            )
        if index < min(len(results), MAX_CARD_DRAMAS) - 1:
            elements.append({"tag": "hr"})

    omitted = len(results) - MAX_CARD_DRAMAS
    if omitted > 0:
        elements.extend(
            [
                {"tag": "hr"},
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"另有 **{omitted}** 部剧未在卡片中展开，请查看任务 JSON 日志。",
                    },
                },
            ]
        )

    timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
    elements.extend(
        [
            {"tag": "hr"},
            {
                "tag": "note",
                "elements": [
                    {
                        "tag": "plain_text",
                        "content": (
                            f"{FEISHU_NOTIFY_CHAT_NAME} · {timestamp} · "
                            "drama-material-hub 自动生成"
                        ),
                    }
                ],
            },
        ]
    )
    return {
        "config": {
            "wide_screen_mode": True,
            "enable_forward": True,
            "update_multi": True,
        },
        "header": {
            "template": template,
            "title": {"tag": "plain_text", "content": title},
        },
        "elements": elements,
    }


def notify_results(
    results: list[dict[str, Any]],
    *,
    task_label: str,
    elapsed_seconds: float,
    enabled: bool,
    only_if_changed: bool = False,
    skipped_ids: list[str] | None = None,
    already_completed_ids: list[str] | None = None,
    ledger_recorded_ids: list[str] | None = None,
) -> dict[str, Any]:
    if not enabled:
        return {"attempted": False, "sent": False, "reason": "disabled"}
    changed = any(item.get("actions") for item in results)
    failures = any(
        item.get("errors") or item.get("status") != "complete" for item in results
    )
    if not results or (only_if_changed and not changed and not failures):
        return {"attempted": False, "sent": False, "reason": "no_changes"}
    try:
        card = build_result_card(
            results,
            task_label=task_label,
            elapsed_seconds=elapsed_seconds,
            skipped_ids=skipped_ids,
            already_completed_ids=already_completed_ids,
            ledger_recorded_ids=ledger_recorded_ids,
        )
        message_id = FeishuClient().send_card(FEISHU_NOTIFY_CHAT_ID, card)
        return {
            "attempted": True,
            "sent": True,
            "chat_name": FEISHU_NOTIFY_CHAT_NAME,
            "chat_id": FEISHU_NOTIFY_CHAT_ID,
            "message_id": message_id,
        }
    except Exception as exc:
        return {
            "attempted": True,
            "sent": False,
            "chat_name": FEISHU_NOTIFY_CHAT_NAME,
            "error": str(exc),
        }


class MaterialHub:
    def __init__(self, *, dry_run: bool = False) -> None:
        self.dry_run = dry_run
        self._metabase: MetabaseClient | None = None
        self._oss: OssStore | None = None
        self._feishu: FeishuClient | None = None
        self._quality_batch: dict[str, dict[str, Any]] | None = None
        self._quality_batch_error: str | None = None

    @property
    def metabase(self) -> MetabaseClient:
        if self._metabase is None:
            self._metabase = MetabaseClient()
        return self._metabase

    @property
    def oss(self) -> OssStore:
        if self._oss is None:
            self._oss = OssStore()
        return self._oss

    @property
    def feishu(self) -> FeishuClient:
        if self._feishu is None:
            self._feishu = FeishuClient()
        return self._feishu

    def local_source(self, drama_id: str) -> Path:
        return AUTO_CUT_ROOT / f"#{drama_id}#" / "source"

    def target_key(self, drama_id: str, relative: str) -> str:
        return f"{TARGET_PREFIX}/{drama_id}/{relative.lstrip('/')}"

    def source_episodes(self, drama_id: str) -> list[dict[str, Any]]:
        prefix = f"{SOURCE_PREFIX}/{drama_id}/"
        episode_numbers: set[int] = set()
        for item in self.oss.list(prefix, source=True):
            name = Path(item["key"]).name
            if Path(name).suffix.lower() != ".mp4":
                continue
            match = VIDEO_RE.search(Path(name).stem)
            if match:
                episode_numbers.add(int(match.group(1)))
        return [
            {
                "label": f"EP{number}",
                "number": number,
                "url": None,
                "publish_at": None,
                "order_num": number,
                "source": "tb_oss_fallback",
            }
            for number in sorted(episode_numbers)[:MAX_PUBLIC_EPISODES]
        ]

    def feishu_episodes(
        self,
        folder_token: str | None,
    ) -> list[dict[str, Any]]:
        if not folder_token:
            return []
        files = self.feishu.video_files(folder_token)
        numbers = sorted(
            {
                int(match.group(1))
                for label in files
                if (match := VIDEO_RE.search(label))
            }
        )[:MAX_PUBLIC_EPISODES]
        return [
            {
                "label": f"EP{number}",
                "number": number,
                "url": None,
                "publish_at": None,
                "order_num": number,
                "source": "supply_deliverable_fallback",
                "feishu_folder_token": folder_token,
            }
            for number in numbers
        ]

    @staticmethod
    def same_local(path: Path, remote_size: int | None) -> bool:
        return bool(path.is_file() and remote_size is not None and path.stat().st_size == remote_size)

    def download_target(
        self,
        key: str,
        path: Path,
        actions: list[str],
        *,
        force: bool = False,
    ) -> None:
        size = self.oss.size(key)
        if size is None:
            return
        if not force and self.same_local(path, size):
            return
        actions.append(f"OSS→本地 {key}")
        if not self.dry_run:
            self.oss.download(key, path)

    def upload_local(
        self,
        key: str,
        path: Path,
        actions: list[str],
        *,
        force: bool = False,
    ) -> None:
        if not path.is_file():
            return
        remote_size = self.oss.size(key)
        if not force and remote_size == path.stat().st_size:
            return
        actions.append(f"本地→OSS {key}")
        if not self.dry_run:
            self.oss.upload_file(key, path)

    def ensure_metadata(
        self,
        drama_id: str,
        info: dict[str, Any],
        episodes: list[dict[str, Any]],
        actions: list[str],
        *,
        force: bool,
    ) -> None:
        title = info.get("title")
        core_data = {
            "id": int(drama_id),
            "drama_id": drama_id,
            "display_title": title,
            "title": title,
            "episode_count": info.get("episode_count"),
            "publish_count": info.get("publish_count"),
            "visible_count": info.get("visible_count"),
            "public_episode_count": len(episodes),
            "public_episodes": [item["label"] for item in episodes],
            "first_pay_episode": info.get("first_pay_episode"),
            "ad_unlock_start_episode": info.get("ad_unlock_start_episode"),
            "is_completion": info.get("is_completion"),
            "copyright": info.get("copyright"),
            "last_publish_at": info.get("last_publish_at"),
            "next_publish_at": info.get("next_publish_at"),
            "create_at": info.get("create_at"),
        }
        key = self.target_key(drama_id, "drama_info.json")
        local = self.local_source(drama_id) / "drama_info.json"
        remote = self.oss.read_bytes(key)
        remote_data: dict[str, Any] = {}
        if remote:
            try:
                remote_data = json.loads(remote.decode("utf-8-sig"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                remote_data = {}
        remote_core = dict(remote_data)
        remote_core.pop("material_synced_at", None)
        synced_at = (
            remote_data.get("material_synced_at")
            if not force and remote_core == core_data
            else now_iso()
        )
        data = {**core_data, "material_synced_at": synced_at}
        payload = (json.dumps(data, ensure_ascii=False, indent=2, default=str) + "\n").encode()
        if force or remote != payload:
            actions.append(f"生成 OSS {key}")
            if not self.dry_run:
                self.oss.upload_bytes(key, payload, "application/json")
        if force or not local.exists() or local.read_bytes() != payload:
            actions.append(f"生成本地 {local}")
            if not self.dry_run:
                local.parent.mkdir(parents=True, exist_ok=True)
                local.write_bytes(payload)

    def ensure_videos(
        self,
        drama_id: str,
        title: str | None,
        episodes: list[dict[str, Any]],
        actions: list[str],
        errors: list[str],
        *,
        force: bool,
        folder_token: str | None = None,
    ) -> None:
        missing: list[dict[str, Any]] = []
        for episode in episodes:
            label = episode["label"]
            target_key = self.target_key(drama_id, f"video/{label}.mp4")
            if not force and self.oss.exists(target_key):
                continue
            source_key = f"{SOURCE_PREFIX}/{drama_id}/{label}.mp4"
            actions.append(f"补正片 {label}")
            if self.dry_run:
                continue
            if self.oss.copy_source(source_key, target_key):
                continue
            url = episode.get("url")
            if url:
                try:
                    with tempfile.TemporaryDirectory(prefix="drama-material-video-") as temp_dir:
                        path = Path(temp_dir) / f"{label}.mp4"
                        self._download_http(url, path)
                        self.oss.upload_file(target_key, path)
                    continue
                except Exception as exc:
                    errors.append(f"{label} 直链失败: {exc}")
            missing.append(episode)

        if missing and not self.dry_run:
            try:
                folder = folder_token or self.feishu.find_drama_folder(
                    drama_id,
                    title,
                )
                files = self.feishu.video_files(folder) if folder else {}
                for episode in missing:
                    label = episode["label"]
                    item = files.get(label)
                    if not item:
                        errors.append(f"{label} 飞书未找到")
                        continue
                    try:
                        with tempfile.TemporaryDirectory(prefix="drama-material-feishu-video-") as temp_dir:
                            path = Path(temp_dir) / f"{label}.mp4"
                            self.feishu.download(item["token"], path)
                            self.oss.upload_file(
                                self.target_key(drama_id, f"video/{label}.mp4"),
                                path,
                            )
                    except Exception as exc:
                        errors.append(f"{label} 飞书正片兜底失败: {exc}")
            except Exception as exc:
                errors.append(f"飞书正片兜底失败: {exc}")

        for episode in episodes:
            label = episode["label"]
            key = self.target_key(drama_id, f"video/{label}.mp4")
            self.download_target(
                key,
                self.local_source(drama_id) / "videos" / f"{label}.mp4",
                actions,
                force=force,
            )

    def ensure_script(
        self,
        drama_id: str,
        title: str | None,
        actions: list[str],
        errors: list[str],
        *,
        force: bool,
        folder_token: str | None = None,
    ) -> None:
        prefix = self.target_key(drama_id, "script/")
        current = [
            item for item in self.oss.list(prefix)
            if Path(item["key"]).suffix.lower() in DOC_EXTENSIONS
        ]
        if force or not current:
            script = self.metabase.script_info(drama_id, title)
            url = script.get("file_url")
            name = safe_filename(
                script.get("file_name") or (Path(urlparse(url).path).name if url else ""),
                f"script_{drama_id}.docx",
            )
            online_document = bool(url and self.feishu.is_online_document_url(url))
            if online_document and Path(name).suffix.lower() not in DOC_EXTENSIONS:
                name = f"{Path(name).stem or '在线文档'}_{drama_id}.docx"
            uploaded = False
            if url:
                actions.append(f"补剧本 {name}")
                if not self.dry_run:
                    try:
                        with tempfile.TemporaryDirectory(prefix="drama-material-script-") as temp_dir:
                            path = Path(temp_dir) / name
                            if online_document:
                                self.feishu.download_online_document(url, path)
                            else:
                                self._download_http(url, path)
                            self.oss.upload_file(f"{prefix}{name}", path)
                        uploaded = True
                    except Exception as exc:
                        errors.append(f"剧本下载失败: {exc}")
            if not uploaded and not self.dry_run:
                try:
                    folder = folder_token or self.feishu.find_drama_folder(
                        drama_id,
                        title,
                    )
                    item = self.feishu.script_file(folder) if folder else None
                    if item:
                        name = safe_filename(item.get("name", ""), f"script_{drama_id}.docx")
                        with tempfile.TemporaryDirectory(prefix="drama-material-feishu-script-") as temp_dir:
                            path = Path(temp_dir) / name
                            self.feishu.download(item["token"], path)
                            self.oss.upload_file(f"{prefix}{name}", path)
                    else:
                        errors.append("剧本在 Metabase 和飞书均未找到")
                except Exception as exc:
                    errors.append(f"飞书剧本兜底失败: {exc}")

        for item in self.oss.list(prefix):
            name = Path(item["key"]).name
            if Path(name).suffix.lower() not in DOC_EXTENSIONS:
                continue
            self.download_target(
                item["key"],
                self.local_source(drama_id) / "docx" / name,
                actions,
                force=force,
            )

    def ensure_suggestions(
        self,
        drama_id: str,
        title: str | None,
        actions: list[str],
        errors: list[str],
        *,
        force: bool,
        folder_token: str | None = None,
    ) -> None:
        key = self.target_key(drama_id, "素材建议.txt")
        local = self.local_source(drama_id) / "素材建议.txt"
        if not force and self.oss.exists(key):
            self.download_target(key, local, actions)
            return

        actions.append("提取素材建议")
        if self.dry_run:
            return
        try:
            direct_url = suggestion_source_url(drama_id)
            with tempfile.TemporaryDirectory(prefix="drama-material-suggestion-") as temp_dir:
                if direct_url:
                    source = Path(temp_dir) / f"素材建议_{drama_id}.docx"
                    self.feishu.download_online_document(direct_url, source)
                else:
                    folder = folder_token or self.feishu.find_drama_folder(
                        drama_id,
                        title,
                    )
                    item = self.feishu.suggestion_file(folder) if folder else None
                    if not item:
                        errors.append("飞书未找到素材建议")
                        return
                    source_name = safe_filename(item.get("name", ""), "素材建议.docx")
                    if item.get("type") == "docx" and Path(source_name).suffix.lower() not in DOC_EXTENSIONS:
                        source_name += ".docx"
                    source = Path(temp_dir) / source_name
                    # Feishu Drive returns online documents as ``type=docx``.
                    # They are not downloadable through the binary Drive
                    # endpoint (which responds 404); read their raw document
                    # content through the docx API instead.
                    if item.get("type") == "docx":
                        self.feishu.download_online_document(
                            f"https://welltop.feishu.cn/docx/{item['token']}",
                            source,
                        )
                    else:
                        self.feishu.download(item["token"], source)
                content = clean_suggestion_text(parse_text_file(source))
            if not content.strip():
                errors.append("素材建议解析后为空")
                return
            payload = content.encode("utf-8")
            self.oss.upload_bytes(key, payload, "text/plain; charset=utf-8")
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_bytes(payload)
        except Exception as exc:
            errors.append(f"素材建议失败: {exc}")

    def ensure_quality(
        self,
        drama_id: str,
        actions: list[str],
        errors: list[str],
        *,
        force: bool,
    ) -> None:
        helper_succeeded = False
        batch_summary = (
            self._quality_batch.get(drama_id)
            if self._quality_batch is not None
            else None
        )
        if self._quality_batch is not None:
            if batch_summary is None:
                errors.append(
                    "优质素材批量同步失败: "
                    f"{self._quality_batch_error or '未返回该剧结果'}"
                )
            else:
                uploaded = int(batch_summary.get("uploaded") or 0)
                local = batch_summary.get("local") or {}
                local_downloaded = int(local.get("downloaded") or 0)
                planned = int(batch_summary.get("planned") or 0)
                local_planned = int(local.get("planned") or 0)
                item_errors = list(batch_summary.get("errors") or [])
                item_errors.extend(local.get("errors") or [])
                if int(batch_summary.get("failed") or 0) or int(local.get("failed") or 0):
                    errors.append(
                        "优质素材同步失败: "
                        + ("；".join(item_errors) if item_errors else "批量任务部分失败")
                    )
                else:
                    helper_succeeded = True
                if uploaded:
                    actions.append(f"补充 OSS 优质素材 {uploaded} 条")
                if local_downloaded:
                    actions.append(f"OSS→本地优质素材 {local_downloaded} 条")
                if self.dry_run and (planned or local_planned):
                    actions.append(
                        f"将补优质素材 OSS {planned} 条、本地 {local_planned} 条"
                    )
        elif not QUALITY_SYNC_SCRIPT.exists():
            errors.append("优质素材同步脚本不存在")
        else:
            command = [
                sys.executable,
                str(QUALITY_SYNC_SCRIPT),
                "--series-ids",
                drama_id,
                "--parent-ledger-lock",
            ]
            if self.dry_run:
                command.append("--dry-run")
            elif force:
                command.append("--force-quality")
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=1800,
            )
            try:
                summary = json.loads(result.stdout)
            except Exception:
                summary = {}
            if result.returncode != 0 or summary.get("status") == "error":
                detail = summary.get("error")
                if not detail:
                    detail = (result.stderr or result.stdout or "")[-500:].strip()
                errors.append(f"优质素材同步失败: {detail}")
            else:
                helper_succeeded = True
                uploaded = int(summary.get("uploaded") or 0)
                local_downloaded = int(summary.get("local_downloaded") or 0)
                planned = int(summary.get("planned") or 0)
                local_planned = int(summary.get("local_planned") or 0)
                if uploaded:
                    actions.append(f"补充 OSS 优质素材 {uploaded} 条")
                if local_downloaded:
                    actions.append(f"OSS→本地优质素材 {local_downloaded} 条")
                if self.dry_run and (planned or local_planned):
                    actions.append(
                        f"将补优质素材 OSS {planned} 条、本地 {local_planned} 条"
                    )
        prefix = self.target_key(drama_id, "优质素材/")
        for item in self.oss.list(prefix):
            if Path(item["key"]).suffix.lower() != ".mp4":
                continue
            self.download_target(
                item["key"],
                self.local_source(drama_id) / "优质素材" / Path(item["key"]).name,
                actions,
                force=force and not helper_succeeded,
            )

    def prepare_quality_batch(
        self,
        drama_ids: list[str],
        *,
        force: bool,
    ) -> None:
        self._quality_batch = {}
        self._quality_batch_error = None
        if not QUALITY_SYNC_SCRIPT.exists():
            self._quality_batch_error = "优质素材同步脚本不存在"
            return
        command = [
            sys.executable,
            str(QUALITY_SYNC_SCRIPT),
            "--series-ids",
            ",".join(drama_ids),
            "--parent-ledger-lock",
        ]
        if self.dry_run:
            command.append("--dry-run")
        elif force:
            command.append("--force-quality")
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=max(1800, 120 * len(drama_ids)),
        )
        try:
            summary = json.loads(result.stdout)
        except Exception:
            summary = {}
        for item in summary.get("items") or []:
            drama_id = str(item.get("drama_id") or "")
            if drama_id:
                self._quality_batch[drama_id] = item
        if result.returncode != 0 or summary.get("status") == "error":
            self._quality_batch_error = (
                summary.get("error")
                or (result.stderr or result.stdout or "")[-500:].strip()
                or "未知错误"
            )

    def inspect(
        self,
        drama_id: str,
        *,
        info: dict[str, Any] | None = None,
        episodes: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        info = info if info is not None else self.metabase.series_info(drama_id)
        folder_token = FeishuClient.folder_token_from_url(
            str(info.get("_deliverable_url") or "") if info else None
        )
        if episodes is None:
            episodes = self.metabase.public_episodes(drama_id)
            if not episodes:
                episodes = self.source_episodes(drama_id)
            if not episodes:
                episodes = self.feishu_episodes(folder_token)
        source = self.local_source(drama_id)
        metadata_key = self.target_key(drama_id, "drama_info.json")
        suggestion_key = self.target_key(drama_id, "素材建议.txt")
        expected_labels = [item["label"] for item in episodes]

        target_videos = {
            Path(item["key"]).stem: item["size"]
            for item in self.oss.list(self.target_key(drama_id, "video/"))
            if Path(item["key"]).suffix.lower() == ".mp4"
        }
        local_videos = {
            path.stem: path.stat().st_size for path in (source / "videos").glob("*.mp4")
        } if (source / "videos").exists() else {}
        target_scripts = {
            Path(item["key"]).name: item["size"]
            for item in self.oss.list(self.target_key(drama_id, "script/"))
            if Path(item["key"]).suffix.lower() in DOC_EXTENSIONS
        }
        local_scripts = {
            path.name: path.stat().st_size for path in (source / "docx").glob("*")
            if path.suffix.lower() in DOC_EXTENSIONS
        } if (source / "docx").exists() else {}
        target_quality = {
            Path(item["key"]).name: item["size"]
            for item in self.oss.list(self.target_key(drama_id, "优质素材/"))
            if Path(item["key"]).suffix.lower() == ".mp4"
        }
        local_quality = {
            path.name: path.stat().st_size for path in (source / "优质素材").glob("*.mp4")
        } if (source / "优质素材").exists() else {}
        expected_video_sizes = {
            label: target_videos.get(label) for label in expected_labels
        }
        videos_match = bool(expected_labels) and all(
            expected_video_sizes[label] is not None
            and local_videos.get(label) == expected_video_sizes[label]
            for label in expected_labels
        )
        scripts_match = bool(target_scripts) and all(
            local_scripts.get(name) == size for name, size in target_scripts.items()
        )
        quality_match = bool(target_quality) and all(
            local_quality.get(name) == size for name, size in target_quality.items()
        )
        metadata_oss_size = self.oss.size(metadata_key)
        suggestion_oss_size = self.oss.size(suggestion_key)
        metadata_local = source / "drama_info.json"
        suggestion_local = source / "素材建议.txt"

        categories = {
            "metadata": {
                "complete": metadata_oss_size is not None
                and metadata_local.is_file()
                and metadata_local.stat().st_size == metadata_oss_size,
                "oss": metadata_oss_size is not None,
                "local": metadata_local.is_file(),
            },
            "videos": {
                "complete": videos_match,
                "expected": expected_labels,
                "oss_count": sum(label in target_videos for label in expected_labels),
                "local_count": sum(label in local_videos for label in expected_labels),
            },
            "script": {
                "complete": scripts_match,
                "oss_count": len(target_scripts),
                "local_count": len(local_scripts),
            },
            "suggestions": {
                "complete": suggestion_oss_size is not None
                and suggestion_oss_size > 0
                and suggestion_local.is_file()
                and suggestion_local.stat().st_size == suggestion_oss_size,
                "oss": suggestion_oss_size is not None,
                "local": suggestion_local.is_file(),
            },
            "quality": {
                "complete": quality_match,
                "oss_count": len(target_quality),
                "local_count": len(local_quality),
            },
        }
        complete = bool(info) and all(item["complete"] for item in categories.values())
        return {
            "drama_id": drama_id,
            "title": info.get("title") if info else None,
            "status": "complete" if complete else "partial",
            "categories": categories,
        }

    def sync_one(
        self,
        drama_id: str,
        *,
        categories: set[str],
        force_categories: set[str],
    ) -> dict[str, Any]:
        actions: list[str] = []
        errors: list[str] = []
        try:
            info = self.metabase.series_info(drama_id)
            folder_token = FeishuClient.folder_token_from_url(
                str(info.get("_deliverable_url") or "") if info else None
            )
            episodes = self.metabase.public_episodes(drama_id)
            if not episodes:
                episodes = self.source_episodes(drama_id)
            if not episodes:
                episodes = self.feishu_episodes(folder_token)
            if not info:
                errors.append("Metabase 未找到剧信息")
            title = info.get("title") if info else None
            if "metadata" in categories:
                self.ensure_metadata(
                    drama_id,
                    info,
                    episodes,
                    actions,
                    force="metadata" in force_categories,
                )
            if "videos" in categories:
                self.ensure_videos(
                    drama_id,
                    title,
                    episodes,
                    actions,
                    errors,
                    force="videos" in force_categories,
                    folder_token=folder_token,
                )
            if "script" in categories:
                self.ensure_script(
                    drama_id,
                    title,
                    actions,
                    errors,
                    force="script" in force_categories,
                    folder_token=folder_token,
                )
            if "suggestions" in categories:
                self.ensure_suggestions(
                    drama_id,
                    title,
                    actions,
                    errors,
                    force="suggestions" in force_categories,
                    folder_token=folder_token,
                )
            if "quality" in categories:
                self.ensure_quality(
                    drama_id,
                    actions,
                    errors,
                    force="quality" in force_categories,
                )
            status = self.inspect(drama_id, info=info, episodes=episodes)
            status["actions"] = actions
            status["errors"] = errors
            status["requested_categories"] = sorted(categories)
            status["dry_run"] = self.dry_run
            return status
        except Exception as exc:
            return {
                "drama_id": drama_id,
                "status": "error",
                "categories": {},
                "actions": actions,
                "errors": errors + [str(exc)],
                "requested_categories": sorted(categories),
                "dry_run": self.dry_run,
            }

    @staticmethod
    def _download_http(url: str, path: Path) -> None:
        session = requests.Session()
        session.trust_env = False
        with session.get(url, stream=True, timeout=(30, 600)) as response:
            response.raise_for_status()
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("wb") as handle:
                for chunk in response.iter_content(1024 * 1024):
                    if chunk:
                        handle.write(chunk)
        if not path.exists() or path.stat().st_size == 0:
            raise HubError("下载结果为空")


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {
            "version": 1,
            "cutover_at": now_iso(),
            "managed_dramas": {},
            "manual_dramas": {},
            "last_discovery_at": None,
            "last_cron_at": None,
        }
    return json.loads(STATE_FILE.read_text(encoding="utf-8"))


def save_state(state: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = STATE_FILE.with_suffix(".json.tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(STATE_FILE)


def default_source_ledger() -> dict[str, Any]:
    return {
        "version": 1,
        "baseline_completed_from": str(MIN_SUPPORTED_DRAMA_ID),
        "baseline_completed_through": str(SOURCE_BASELINE_COMPLETED_THROUGH),
        "completed": {},
    }


def load_source_ledger() -> dict[str, Any]:
    if not SOURCE_LEDGER_FILE.exists():
        return default_source_ledger()
    try:
        ledger = json.loads(SOURCE_LEDGER_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HubError(f"source同步记录表损坏: {SOURCE_LEDGER_FILE}: {exc}") from exc
    if not isinstance(ledger, dict) or not isinstance(ledger.get("completed"), dict):
        raise HubError(f"source同步记录表格式错误: {SOURCE_LEDGER_FILE}")
    ledger.setdefault("version", 1)
    ledger.setdefault("baseline_completed_from", str(MIN_SUPPORTED_DRAMA_ID))
    ledger.setdefault(
        "baseline_completed_through",
        str(SOURCE_BASELINE_COMPLETED_THROUGH),
    )
    return ledger


def save_source_ledger(ledger: dict[str, Any]) -> None:
    SOURCE_LEDGER_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = SOURCE_LEDGER_FILE.with_suffix(".json.tmp")
    temp.write_text(
        json.dumps(ledger, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temp.replace(SOURCE_LEDGER_FILE)


@contextmanager
def source_ledger_lock() -> Iterable[None]:
    SOURCE_LEDGER_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with SOURCE_LEDGER_LOCK_FILE.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def with_source_ledger_lock(function: Any) -> Any:
    @wraps(function)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        with source_ledger_lock():
            return function(*args, **kwargs)

    return wrapped


def source_completion_record(
    drama_id: str,
    ledger: dict[str, Any],
) -> dict[str, Any] | None:
    baseline_from = int(
        ledger.get("baseline_completed_from") or MIN_SUPPORTED_DRAMA_ID
    )
    baseline_through = int(
        ledger.get("baseline_completed_through")
        or SOURCE_BASELINE_COMPLETED_THROUGH
    )
    if baseline_from <= int(drama_id) <= baseline_through:
        return {
            "source": "baseline",
            "completed": True,
            "baseline_completed_from": str(baseline_from),
            "baseline_completed_through": str(baseline_through),
        }
    record = (ledger.get("completed") or {}).get(drama_id)
    if isinstance(record, dict):
        return {"source": "record", "completed": True, **record}
    return None


def partition_source_completed(
    drama_ids: Iterable[str],
    ledger: dict[str, Any],
) -> tuple[list[str], list[str]]:
    pending: list[str] = []
    completed: list[str] = []
    for drama_id in drama_ids:
        target = completed if source_completion_record(drama_id, ledger) else pending
        target.append(drama_id)
    return pending, completed


def record_source_completions(
    ledger: dict[str, Any],
    results: list[dict[str, Any]],
    *,
    automatic: bool,
) -> tuple[bool, list[str]]:
    completed = ledger.setdefault("completed", {})
    changed = False
    newly_recorded: list[str] = []
    timestamp = now_iso()
    baseline_through = int(
        ledger.get("baseline_completed_through")
        or SOURCE_BASELINE_COMPLETED_THROUGH
    )
    for result in results:
        drama_id = str(result.get("drama_id") or "")
        if (
            result.get("status") != "complete"
            or not is_supported_drama_id(drama_id)
            or int(drama_id) <= baseline_through
        ):
            continue
        record = completed.get(drama_id)
        if not isinstance(record, dict):
            record = {
                "completed_at": timestamp,
                "mode": "automatic" if automatic else "manual",
                "oss_prefix": f"{TARGET_PREFIX}/{drama_id}/",
                "local_source": str(
                    AUTO_CUT_ROOT / f"#{drama_id}#" / "source"
                ),
            }
            completed[drama_id] = record
            newly_recorded.append(drama_id)
        record["last_confirmed_at"] = timestamp
        changed = True
    if changed:
        ledger["updated_at"] = timestamp
    return changed, newly_recorded


def update_state_for_results(
    state: dict[str, Any],
    results: list[dict[str, Any]],
    *,
    automatic: bool,
) -> None:
    collection_name = "managed_dramas" if automatic else "manual_dramas"
    managed = state.setdefault(collection_name, {})
    for result in results:
        drama_id = result["drama_id"]
        entry = managed.setdefault(drama_id, {})
        if automatic:
            entry.setdefault("managed_since", now_iso())
        entry.update(
            {
                "status": result.get("status"),
                "categories": result.get("categories", {}),
                "last_sync_at": now_iso(),
                "last_errors": result.get("errors", []),
            }
        )


def exit_code(results: list[dict[str, Any]]) -> int:
    if any(item.get("status") == "error" for item in results):
        return 1
    if any(item.get("status") != "complete" for item in results):
        return 2
    return 0


def notification_failed(notification: dict[str, Any]) -> bool:
    return bool(notification.get("attempted") and not notification.get("sent"))


def print_results(
    results: list[dict[str, Any]],
    *,
    cron: bool = False,
    notification: dict[str, Any] | None = None,
    skipped_ids: list[str] | None = None,
    already_completed_ids: list[str] | None = None,
    ledger_recorded_ids: list[str] | None = None,
) -> None:
    if cron:
        changed = any(item.get("actions") for item in results)
        failures = any(item.get("errors") or item.get("status") != "complete" for item in results)
        if not changed and not failures and not notification_failed(notification or {}):
            print("NO_REPLY")
            return
    payload: dict[str, Any] = {
        "count": len(results),
        "complete": sum(item.get("status") == "complete" for item in results),
        "partial": sum(item.get("status") == "partial" for item in results),
        "error": sum(item.get("status") == "error" for item in results),
        "items": results,
    }
    all_skipped_ids: list[str] = []
    skipped: list[dict[str, str]] = []
    for drama_id in skipped_ids or []:
        if drama_id not in all_skipped_ids:
            all_skipped_ids.append(drama_id)
            skipped.append(
                {
                    "drama_id": drama_id,
                    "reason": EXCLUDED_DRAMA_ID_REASON,
                }
            )
    for drama_id in already_completed_ids or []:
        if drama_id not in all_skipped_ids:
            all_skipped_ids.append(drama_id)
            skipped.append(
                {
                    "drama_id": drama_id,
                    "reason": SOURCE_ALREADY_COMPLETED_REASON,
                }
            )
    if all_skipped_ids:
        payload["skipped_ids"] = all_skipped_ids
        payload["skipped"] = skipped
        if skipped_ids and already_completed_ids:
            payload["skip_reason"] = "包含不合规ID和已完成source的ID，详见skipped"
        elif already_completed_ids:
            payload["skip_reason"] = SOURCE_ALREADY_COMPLETED_REASON
        else:
            payload["skip_reason"] = EXCLUDED_DRAMA_ID_REASON
    if already_completed_ids:
        payload["already_completed_ids"] = already_completed_ids
    if ledger_recorded_ids:
        payload["ledger_recorded_ids"] = ledger_recorded_ids
        payload["ledger_file"] = str(SOURCE_LEDGER_FILE)
    if notification is not None:
        payload["notification"] = notification
    print(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )


@with_source_ledger_lock
def run_sync(args: argparse.Namespace) -> int:
    global MAX_PUBLIC_EPISODES
    limit = getattr(args, "episode_limit", 10)
    if not 1 <= limit <= 500:
        raise HubError("episode-limit 必须为 1～500")
    MAX_PUBLIC_EPISODES = limit
    started = time.monotonic()
    drama_ids, skipped_ids = parse_drama_id_selection(
        args.drama_id,
        allow_legacy=args.allow_legacy,
    )
    force_categories = parse_categories(args.force) if args.force else set()
    if args.only:
        categories = parse_categories(args.only)
    elif force_categories:
        categories = set(force_categories)
    else:
        categories = set(CATEGORIES)
    categories |= force_categories
    source_ledger = load_source_ledger()
    already_completed_ids: list[str] = []
    if not force_categories and not getattr(args, "restore_missing", False):
        drama_ids, already_completed_ids = partition_source_completed(
            drama_ids,
            source_ledger,
        )
    hub = MaterialHub(dry_run=args.dry_run)
    if "quality" in categories and len(drama_ids) > 1:
        hub.prepare_quality_batch(
            drama_ids,
            force="quality" in force_categories,
        )
    results = [
        hub.sync_one(
            drama_id,
            categories=categories,
            force_categories=force_categories,
        )
        for drama_id in drama_ids
    ]
    ledger_recorded_ids: list[str] = []
    if results and not args.dry_run:
        ledger_changed, ledger_recorded_ids = record_source_completions(
            source_ledger,
            results,
            automatic=False,
        )
        if ledger_changed:
            save_source_ledger(source_ledger)
        state = load_state()
        update_state_for_results(state, results, automatic=False)
        save_state(state)
    notification = notify_results(
        results,
        task_label="临时任务",
        elapsed_seconds=time.monotonic() - started,
        enabled=not args.dry_run and not args.no_notify,
        skipped_ids=skipped_ids,
        already_completed_ids=already_completed_ids,
        ledger_recorded_ids=ledger_recorded_ids,
    )
    print_results(
        results,
        notification=notification,
        skipped_ids=skipped_ids,
        already_completed_ids=already_completed_ids,
        ledger_recorded_ids=ledger_recorded_ids,
    )
    if notification_failed(notification):
        return 1
    return exit_code(results)


def run_check(args: argparse.Namespace) -> int:
    drama_ids, skipped_ids = parse_drama_id_selection(args.drama_id)
    source_ledger = load_source_ledger()
    hub = MaterialHub(dry_run=True)
    results: list[dict[str, Any]] = []
    for drama_id in drama_ids:
        try:
            result = hub.inspect(drama_id)
            result.update({"actions": [], "errors": [], "dry_run": True})
        except Exception as exc:
            result = {
                "drama_id": drama_id,
                "status": "error",
                "categories": {},
                "actions": [],
                "errors": [str(exc)],
                "dry_run": True,
            }
        result["source_sync_record"] = (
            source_completion_record(drama_id, source_ledger)
            or {"completed": False}
        )
        results.append(result)
    print_results(results, skipped_ids=skipped_ids)
    return exit_code(results)


def run_ledger(args: argparse.Namespace) -> int:
    ledger = load_source_ledger()
    payload: dict[str, Any] = {
        "ledger_file": str(SOURCE_LEDGER_FILE),
        "baseline_completed_from": ledger.get("baseline_completed_from"),
        "baseline_completed_through": ledger.get(
            "baseline_completed_through"
        ),
        "explicit_record_count": len(ledger.get("completed") or {}),
    }
    if args.drama_id:
        drama_ids, skipped_ids = parse_drama_id_selection(args.drama_id)
        payload["items"] = [
            {
                "drama_id": drama_id,
                "recorded": bool(source_completion_record(drama_id, ledger)),
                "record": source_completion_record(drama_id, ledger),
            }
            for drama_id in drama_ids
        ]
        if skipped_ids:
            payload["skipped_ids"] = skipped_ids
            payload["skip_reason"] = EXCLUDED_DRAMA_ID_REASON
    else:
        payload["completed"] = ledger.get("completed") or {}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


@with_source_ledger_lock
def run_automatic(args: argparse.Namespace, *, cron: bool) -> int:
    started = time.monotonic()
    state = load_state()
    source_ledger = load_source_ledger()
    hub = MaterialHub(dry_run=args.dry_run)
    try:
        discovered = hub.metabase.discover_new(state["cutover_at"])
    except Exception as exc:
        results = [{
            "drama_id": "discovery",
            "status": "error",
            "categories": {},
            "actions": [],
            "errors": [str(exc)],
            "dry_run": args.dry_run,
        }]
        notification = notify_results(
            results,
            task_label="定时任务" if cron else "新剧同步",
            elapsed_seconds=time.monotonic() - started,
            enabled=not args.dry_run and not args.no_notify,
        )
        print_results(
            results,
            cron=cron,
            notification=notification,
        )
        return 1

    managed = state.setdefault("managed_dramas", {})
    skipped_ids: list[str] = []
    already_completed_ids: list[str] = []
    for item in discovered:
        drama_id = normalize_drama_id(item["drama_id"])
        if not is_supported_drama_id(drama_id):
            if drama_id not in skipped_ids:
                skipped_ids.append(drama_id)
            continue
        if source_completion_record(drama_id, source_ledger):
            if drama_id not in already_completed_ids:
                already_completed_ids.append(drama_id)
            continue
        managed.setdefault(
            drama_id,
            {
                "managed_since": now_iso(),
                "first_public_at": item.get("first_public_at"),
                "status": "pending",
            },
        )

    drama_ids: list[str] = []
    for raw_drama_id in managed:
        drama_id = normalize_drama_id(raw_drama_id)
        if not is_supported_drama_id(drama_id):
            if drama_id not in skipped_ids:
                skipped_ids.append(drama_id)
            continue
        if source_completion_record(drama_id, source_ledger):
            if drama_id not in already_completed_ids:
                already_completed_ids.append(drama_id)
            continue
        drama_ids.append(drama_id)
    drama_ids.sort(key=int)
    if not drama_ids:
        print_results(
            [],
            cron=cron,
            skipped_ids=skipped_ids,
            already_completed_ids=already_completed_ids,
        )
        if not args.dry_run:
            state["last_discovery_at"] = now_iso()
            if cron:
                state["last_cron_at"] = now_iso()
            save_state(state)
        return 0

    if len(drama_ids) > 1:
        hub.prepare_quality_batch(drama_ids, force=False)
    results = [
        hub.sync_one(
            drama_id,
            categories=set(CATEGORIES),
            force_categories=set(),
        )
        for drama_id in drama_ids
    ]
    ledger_recorded_ids: list[str] = []
    if not args.dry_run:
        ledger_changed, ledger_recorded_ids = record_source_completions(
            source_ledger,
            results,
            automatic=True,
        )
        if ledger_changed:
            save_source_ledger(source_ledger)
        update_state_for_results(state, results, automatic=True)
        state["last_discovery_at"] = now_iso()
        if cron:
            state["last_cron_at"] = now_iso()
        save_state(state)
    notification = notify_results(
        results,
        task_label="定时任务" if cron else "新剧同步",
        elapsed_seconds=time.monotonic() - started,
        enabled=not args.dry_run and not args.no_notify,
        only_if_changed=cron,
        skipped_ids=skipped_ids,
        already_completed_ids=already_completed_ids,
        ledger_recorded_ids=ledger_recorded_ids,
    )
    print_results(
        results,
        cron=cron,
        notification=notification,
        skipped_ids=skipped_ids,
        already_completed_ids=already_completed_ids,
        ledger_recorded_ids=ledger_recorded_ids,
    )
    if notification_failed(notification):
        return 1
    return exit_code(results)


def episode_label(value: str) -> str:
    text = str(value).strip()
    match = re.fullmatch(r"(?i)ep(\d+)", text) or re.fullmatch(r"(\d+)", text)
    if not match:
        raise HubError(f"无效集号: {value}")
    return f"EP{int(match.group(1))}"


def run_fetch_episode(args: argparse.Namespace) -> int:
    """Download one episode file to dest. Used by Exact Cut as drama_id episode dest."""
    drama_id = normalize_drama_id(args.drama_id)
    label = episode_label(args.episode)
    dest = Path(args.dest)
    hub = MaterialHub(dry_run=False)
    target_key = hub.target_key(drama_id, f"video/{label}.mp4")
    if not hub.oss.exists(target_key):
        source_key = f"{SOURCE_PREFIX}/{drama_id}/{label}.mp4"
        if not hub.oss.copy_source(source_key, target_key):
            raise HubError(f"{label} 源片不存在")
    hub.oss.download(target_key, dest)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="同步短剧完整物料到 OSS 与本地 auto-cut"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    sync_parser = subparsers.add_parser("sync", help="同步单剧或批量剧")
    sync_parser.add_argument(
        "--drama-id",
        action="append",
        required=True,
        help="剧ID；可重复、逗号或空格分隔",
    )
    sync_parser.add_argument("--episode-limit", type=int, default=10, help="本次正片获取上限，默认10，剪辑引用后续集时可扩展，最大500")
    sync_parser.add_argument("--only", help="仅同步指定类别")
    sync_parser.add_argument("--force", help="重新获取指定类别")
    sync_parser.add_argument("--restore-missing", action="store_true",
                             help="显式恢复场景：忽略项目完成台账，仍按文件去重补齐，不强制覆盖已有文件")
    sync_parser.add_argument(
        "--allow-legacy",
        action="store_true",
        help="仅本次放行旧项目 101018 和 101026；不影响普通任务或 Cron",
    )
    sync_parser.add_argument("--dry-run", action="store_true", help="只读预检")
    sync_parser.add_argument(
        "--no-notify",
        action="store_true",
        help=f"不向飞书群「{FEISHU_NOTIFY_CHAT_NAME}」发送结果卡片",
    )

    check_parser = subparsers.add_parser("check", help="只读检查完整性")
    check_parser.add_argument("--drama-id", action="append", required=True)

    status_parser = subparsers.add_parser("status", help="只读查看完整性")
    status_parser.add_argument("--drama-id", action="append", required=True)

    ledger_parser = subparsers.add_parser(
        "ledger",
        help="只读查看source同步记录表",
    )
    ledger_parser.add_argument(
        "--drama-id",
        action="append",
        help="可选；查看指定剧ID是否已记录",
    )

    sync_new_parser = subparsers.add_parser("sync-new", help="同步切换后的新剧")
    sync_new_parser.add_argument("--dry-run", action="store_true", help="只读预检")
    sync_new_parser.add_argument(
        "--no-notify",
        action="store_true",
        help=f"不向飞书群「{FEISHU_NOTIFY_CHAT_NAME}」发送结果卡片",
    )

    cron_parser = subparsers.add_parser("cron", help="Cron 完整性对账")
    cron_parser.add_argument("--dry-run", action="store_true", help="只读预检")
    cron_parser.add_argument(
        "--no-notify",
        action="store_true",
        help=f"不向飞书群「{FEISHU_NOTIFY_CHAT_NAME}」发送结果卡片",
    )

    fetch_parser = subparsers.add_parser(
        "fetch-episode",
        help="把一集正片下载到指定路径",
    )
    fetch_parser.add_argument("drama_id")
    fetch_parser.add_argument("episode")
    fetch_parser.add_argument("dest")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    started = time.monotonic()
    try:
        if args.command == "sync":
            code = run_sync(args)
        elif args.command in {"check", "status"}:
            code = run_check(args)
        elif args.command == "ledger":
            code = run_ledger(args)
        elif args.command == "sync-new":
            code = run_automatic(args, cron=False)
        elif args.command == "cron":
            code = run_automatic(args, cron=True)
        elif args.command == "fetch-episode":
            code = run_fetch_episode(args)
        else:
            parser.error(f"未知命令: {args.command}")
            return 1
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 1
    finally:
        elapsed = time.monotonic() - started
        if os.getenv("DRAMA_MATERIAL_DEBUG") == "1":
            print(f"[debug] elapsed={elapsed:.2f}s", file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
