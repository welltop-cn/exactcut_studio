#!/usr/bin/env python3
"""Discover quality creatives and idempotently mirror them to OSS and auto-cut."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import sys
import tempfile
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import oss2
import requests


HOME = Path.home()
SKILL_DIR = Path(__file__).resolve().parent.parent
SOURCE_LEDGER_FILE = SKILL_DIR / "data" / "source_sync_ledger.json"
SOURCE_LEDGER_LOCK_FILE = SKILL_DIR / "data" / "source_sync_ledger.lock"
METABASE_ENV = HOME / ".openclaw/workspace/.secrets/metabase.env"
OSS_ENV = HOME / ".openclaw/workspace/skills/oss-manager/.env"
CF_ENV = HOME / ".openclaw/workspace/.secrets/creativefitting.env"
AUTO_CUT_ROOT = HOME / "Downloads/auto-cut/projects"

CF_BASE = "https://cf-ads.creativefitting.ai"
QUALITY_DIR = "优质素材"
TARGET_PREFIX = "assets"
MANIFEST_NAME = ".manifest.json"
MIN_VIDEO_BYTES = 1024
TZ_SH = timezone(timedelta(hours=8))
UPLOAD_SUFFIX_RE = re.compile(r"_(20\d{6}_\d{6})$")
SUPPORTED_DRAMA_ID_DIGITS = 6
MIN_SUPPORTED_DRAMA_ID = 101050
MAX_SUPPORTED_DRAMA_ID = 999999
EXCLUDED_DRAMA_ID_REASON = "仅处理6位且在101050～999999范围内的剧ID"
SOURCE_BASELINE_COMPLETED_THROUGH = 101426
SOURCE_ALREADY_COMPLETED_REASON = "已在source同步记录表中，跳过重复下载和上传"


class QualitySyncError(RuntimeError):
    """Quality-material integration failure."""


def now_iso() -> str:
    return datetime.now(TZ_SH).isoformat(timespec="seconds")


def log(message: str) -> None:
    timestamp = datetime.now(TZ_SH).strftime("%H:%M:%S")
    print(f"[{timestamp}] {message}", file=sys.stderr, flush=True)


def load_env(path: Path) -> dict[str, str]:
    if not path.exists():
        raise QualitySyncError(f"配置文件不存在: {path}")
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'").strip('"')
    return values


def load_source_ledger() -> dict[str, Any]:
    if not SOURCE_LEDGER_FILE.exists():
        return {
            "baseline_completed_from": str(MIN_SUPPORTED_DRAMA_ID),
            "baseline_completed_through": str(
                SOURCE_BASELINE_COMPLETED_THROUGH
            ),
            "completed": {},
        }
    try:
        ledger = json.loads(SOURCE_LEDGER_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise QualitySyncError(
            f"source同步记录表损坏: {SOURCE_LEDGER_FILE}: {exc}"
        ) from exc
    if not isinstance(ledger, dict) or not isinstance(ledger.get("completed"), dict):
        raise QualitySyncError(f"source同步记录表格式错误: {SOURCE_LEDGER_FILE}")
    return ledger


@contextmanager
def source_ledger_lock():
    SOURCE_LEDGER_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with SOURCE_LEDGER_LOCK_FILE.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def source_is_completed(drama_id: str, ledger: dict[str, Any]) -> bool:
    baseline_from = int(
        ledger.get("baseline_completed_from") or MIN_SUPPORTED_DRAMA_ID
    )
    baseline_through = int(
        ledger.get("baseline_completed_through")
        or SOURCE_BASELINE_COMPLETED_THROUGH
    )
    return (
        baseline_from <= int(drama_id) <= baseline_through
        or drama_id in (ledger.get("completed") or {})
    )


def add_skip_summary(
    payload: dict[str, Any],
    skipped_ids: list[str],
    already_completed_ids: list[str],
) -> None:
    all_skipped_ids = list(dict.fromkeys(skipped_ids + already_completed_ids))
    if not all_skipped_ids:
        return
    payload["skipped_ids"] = all_skipped_ids
    payload["skipped"] = [
        *[
            {"drama_id": drama_id, "reason": EXCLUDED_DRAMA_ID_REASON}
            for drama_id in skipped_ids
        ],
        *[
            {
                "drama_id": drama_id,
                "reason": SOURCE_ALREADY_COMPLETED_REASON,
            }
            for drama_id in already_completed_ids
        ],
    ]
    if skipped_ids and already_completed_ids:
        payload["skip_reason"] = "包含不合规ID和已完成source的ID，详见skipped"
    elif already_completed_ids:
        payload["skip_reason"] = SOURCE_ALREADY_COMPLETED_REASON
    else:
        payload["skip_reason"] = EXCLUDED_DRAMA_ID_REASON
    if already_completed_ids:
        payload["already_completed_ids"] = already_completed_ids


def parse_series_ids(raw: str) -> tuple[list[str], list[str]]:
    included: list[str] = []
    excluded: list[str] = []
    for value in re.split(r"[\s,，]+", raw.strip()):
        drama_id = value.strip().strip("#")
        if not drama_id:
            continue
        if not re.fullmatch(r"\d{4,12}", drama_id):
            raise argparse.ArgumentTypeError(f"无效剧ID: {value}")
        target = (
            included
            if (
                len(drama_id) == SUPPORTED_DRAMA_ID_DIGITS
                and MIN_SUPPORTED_DRAMA_ID
                <= int(drama_id)
                <= MAX_SUPPORTED_DRAMA_ID
            )
            else excluded
        )
        if drama_id not in target:
            target.append(drama_id)
    if not included and not excluded:
        raise argparse.ArgumentTypeError("至少提供一个剧ID")
    return included, excluded


def normalize_identity(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value or "").casefold().strip()
    normalized = re.sub(r"\.mp4$", "", normalized)
    return re.sub(r"[\s_—\-]+", "_", normalized).strip("_")


def safe_mp4_name(value: str) -> str:
    name = Path((value or "").replace("\x00", "").strip()).name
    if not name:
        raise QualitySyncError("creativefitting 返回空文件名")
    if Path(name).suffix.lower() != ".mp4":
        name = f"{name}.mp4"
    return name


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def metabase_session(config: dict[str, str]) -> str:
    session = requests.Session()
    # Metabase is reachable directly from this host.  Do not inherit the
    # macOS system proxy here; its stale localhost port breaks the request.
    session.trust_env = False
    response = session.post(
        f"{config['METABASE_HOST']}/api/session",
        json={
            "username": config["METABASE_USER"],
            "password": config["METABASE_PASSWORD"],
        },
        timeout=20,
    )
    response.raise_for_status()
    token = response.json().get("id")
    if not token:
        raise QualitySyncError("Metabase 登录未返回 session id")
    return token


def get_quality_materials(
    config: dict[str, str],
    token: str,
    series_ids: list[str],
) -> list[dict[str, Any]]:
    ids_sql = ",".join(series_ids)
    sql = f"""
WITH base_agg AS (
    SELECT
        platform,
        REGEXP_EXTRACT(ad_name, '#([0-9]+)#', 1) AS series_id,
        ad_name,
        SUM(spend) AS spend,
        SUM(conversion) AS conversion
    FROM (
        SELECT 'fb' AS platform, ad_name, spend, purchase AS conversion
        FROM toc_marketing_insight
        WHERE orig_date >= '2026-01-01'
        UNION ALL
        SELECT
            CASE WHEN advertiser_name LIKE '%TTMINIS%' THEN 'ttminis' ELSE 'tt' END,
            ad_name,
            spend,
            conversion
        FROM toc_marketing_insight_tt
        WHERE orig_date >= '2026-01-01'
    ) raw
    WHERE REGEXP_EXTRACT(ad_name, '#([0-9]+)#', 1) IS NOT NULL
      AND CAST(REGEXP_EXTRACT(ad_name, '#([0-9]+)#', 1) AS SIGNED)
          IN ({ids_sql})
    GROUP BY platform, series_id, ad_name
),
ranked AS (
    SELECT *, ROUND(spend / NULLIF(conversion, 0), 2) AS cpa
    FROM base_agg
),
labeled AS (
    SELECT *,
        CASE
            WHEN conversion > 0
             AND cpa <= CASE platform
                 WHEN 'fb' THEN 58
                 WHEN 'ttminis' THEN 43
                 WHEN 'tt' THEN 43
             END
             AND (
                 spend >= CASE platform
                     WHEN 'fb' THEN 282
                     WHEN 'ttminis' THEN 341
                     WHEN 'tt' THEN 341
                 END
                 OR conversion >= 10
             )
            THEN '好'
            WHEN conversion = 0
              OR cpa >= CASE platform
                  WHEN 'fb' THEN 115
                  WHEN 'ttminis' THEN 103
                  WHEN 'tt' THEN 103
              END
            THEN '差'
            ELSE '中'
        END AS quality
    FROM ranked
)
SELECT
    platform,
    series_id,
    ad_name,
    ROUND(spend, 2) AS spend,
    conversion,
    cpa,
    quality
FROM labeled
WHERE quality = '好'
ORDER BY series_id, spend DESC
"""
    session = requests.Session()
    session.trust_env = False
    response = session.post(
        f"{config['METABASE_HOST']}/api/dataset",
        headers={"X-Metabase-Session": token},
        json={"database": 2, "type": "native", "native": {"query": sql}},
        timeout=120,
    )
    response.raise_for_status()
    payload = response.json()
    data = payload.get("data", {})
    columns = [column.get("name") for column in data.get("cols", [])]
    rows = [dict(zip(columns, row)) for row in data.get("rows", [])]

    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        drama_id = str(row.get("series_id") or "")
        ad_name = str(row.get("ad_name") or "")
        key = (drama_id, normalize_identity(ad_name))
        if drama_id and key[1] and key not in unique:
            unique[key] = row
    return list(unique.values())


def cf_login(config: dict[str, str]) -> tuple[requests.Session, str]:
    session = requests.Session()
    session.trust_env = False
    response = session.post(
        f"{CF_BASE}/api/v1/auth/login",
        json={
            "username": config["CF_ADS_USER"],
            "password": config["CF_ADS_PASSWORD"],
        },
        timeout=20,
    )
    response.raise_for_status()
    token = response.json().get("data", {}).get("token")
    if not token:
        raise QualitySyncError("creativefitting 登录未返回 token")
    return session, token


def cf_materials(
    session: requests.Session,
    token: str,
    drama_id: str,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for page in range(1, 21):
        response = session.get(
            f"{CF_BASE}/api/v1/material/list",
            headers={"Authorization": f"Bearer {token}"},
            params={
                "keyword": drama_id,
                "page": page,
                "size": 100,
            },
            timeout=30,
        )
        response.raise_for_status()
        data = response.json().get("data", {})
        batch = data.get("material_list", [])
        if not batch:
            break
        items.extend(batch)
        if not data.get("has_more"):
            break
    return items


def match_cf_material(
    drama_id: str,
    ad_name: str,
    materials: list[dict[str, Any]],
) -> dict[str, Any] | None:
    wanted = normalize_identity(ad_name)
    wanted_no_id = normalize_identity(re.sub(r"#\d+#", "", ad_name))
    for item in materials:
        item_drama_id = str(item.get("drama_id") or "")
        if item_drama_id and item_drama_id != drama_id:
            continue
        filename = str(item.get("file_name") or "")
        if normalize_identity(filename) == wanted:
            return item
    for item in materials:
        item_drama_id = str(item.get("drama_id") or "")
        if item_drama_id and item_drama_id != drama_id:
            continue
        filename = re.sub(r"#\d+#", "", str(item.get("file_name") or ""))
        if wanted_no_id and normalize_identity(filename) == wanted_no_id:
            return item
    return None


def make_bucket(config: dict[str, str]) -> oss2.Bucket:
    required = (
        "ALIYUN_OSS_ACCESS_KEY_ID",
        "ALIYUN_OSS_ACCESS_KEY_SECRET",
        "ALIYUN_OSS_ENDPOINT",
        "ALIYUN_OSS_BUCKET",
    )
    if not all(config.get(key) for key in required):
        raise QualitySyncError("OSS 配置缺失")
    bucket = oss2.Bucket(
        oss2.Auth(
            config["ALIYUN_OSS_ACCESS_KEY_ID"],
            config["ALIYUN_OSS_ACCESS_KEY_SECRET"],
        ),
        config["ALIYUN_OSS_ENDPOINT"],
        config["ALIYUN_OSS_BUCKET"],
        connect_timeout=30,
    )
    # OSS traffic in this environment requires the configured local proxy.
    bucket.session.session.trust_env = True
    return bucket


def quality_prefix(drama_id: str) -> str:
    return f"{TARGET_PREFIX}/{drama_id}/{QUALITY_DIR}/"


def list_quality_objects(
    bucket: oss2.Bucket,
    drama_id: str,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in oss2.ObjectIterator(bucket, prefix=quality_prefix(drama_id)):
        if not item.key.lower().endswith(".mp4"):
            continue
        result.append({"key": item.key, "size": int(item.size)})
    return result


def empty_manifest() -> dict[str, Any]:
    return {"version": 2, "updated_at": None, "items": {}}


def load_manifest(bucket: oss2.Bucket, drama_id: str) -> dict[str, Any]:
    key = f"{quality_prefix(drama_id)}{MANIFEST_NAME}"
    try:
        payload = json.loads(bucket.get_object(key).read())
    except Exception:
        return empty_manifest()
    if (
        isinstance(payload, dict)
        and payload.get("version") == 2
        and isinstance(payload.get("items"), dict)
    ):
        return payload

    manifest = empty_manifest()
    if isinstance(payload, dict):
        for original_name, stored_name in payload.items():
            if not isinstance(original_name, str):
                continue
            stored = stored_name if isinstance(stored_name, str) else original_name
            oss_key = (
                stored
                if stored.startswith(f"{TARGET_PREFIX}/")
                else f"{quality_prefix(drama_id)}{Path(stored).name}"
            )
            manifest["items"][f"file:{normalize_identity(original_name)}"] = {
                "source_file_name": original_name,
                "oss_key": oss_key,
                "migrated_from_v1": True,
            }
    return manifest


def save_manifest(
    bucket: oss2.Bucket,
    drama_id: str,
    manifest: dict[str, Any],
) -> None:
    manifest["version"] = 2
    manifest["updated_at"] = now_iso()
    payload = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
    bucket.put_object(
        f"{quality_prefix(drama_id)}{MANIFEST_NAME}",
        payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
    )


def material_aliases(
    ad_name: str,
    filename: str,
    material: dict[str, Any],
) -> list[str]:
    aliases = [
        f"ad:{normalize_identity(ad_name)}",
        f"file:{normalize_identity(filename)}",
    ]
    material_id = material.get("id") or material.get("material_id")
    if material_id is not None:
        aliases.append(f"cf:{material_id}")
    return list(dict.fromkeys(alias for alias in aliases if not alias.endswith(":")))


def record_aliases(
    manifest: dict[str, Any],
    aliases: list[str],
    record: dict[str, Any],
) -> None:
    for alias in aliases:
        manifest.setdefault("items", {})[alias] = dict(record)


def manifest_existing_key(
    bucket: oss2.Bucket,
    manifest: dict[str, Any],
    aliases: list[str],
) -> str | None:
    for alias in aliases:
        record = manifest.get("items", {}).get(alias)
        key = record.get("oss_key") if isinstance(record, dict) else None
        if key and bucket.object_exists(key):
            return key
    return None


def filename_existing_key(
    filename: str,
    objects: list[dict[str, Any]],
) -> str | None:
    wanted = safe_mp4_name(filename)
    wanted_stem = Path(wanted).stem
    exact: list[str] = []
    legacy: list[str] = []
    for item in objects:
        name = Path(item["key"]).name
        if name == wanted:
            exact.append(item["key"])
            continue
        stem = Path(name).stem
        if stem.startswith(f"{wanted_stem}_"):
            suffix = stem[len(wanted_stem) :]
            if UPLOAD_SUFFIX_RE.fullmatch(suffix):
                legacy.append(item["key"])
    return (sorted(exact) or sorted(legacy) or [None])[0]


def hash_existing_key(
    bucket: oss2.Bucket,
    manifest: dict[str, Any],
    digest: str,
) -> str | None:
    checked: set[str] = set()
    for record in manifest.get("items", {}).values():
        if not isinstance(record, dict) or record.get("sha256") != digest:
            continue
        key = record.get("oss_key")
        if key and key not in checked and bucket.object_exists(key):
            return key
        if key:
            checked.add(key)
    return None


def download_source(
    session: requests.Session,
    url: str,
    path: Path,
) -> int:
    if not url:
        raise QualitySyncError("creativefitting 素材缺少 oss_url")
    with session.get(url, stream=True, timeout=(30, 600)) as response:
        response.raise_for_status()
        with path.open("wb") as handle:
            for chunk in response.iter_content(1024 * 1024):
                if chunk:
                    handle.write(chunk)
    size = path.stat().st_size
    if size < MIN_VIDEO_BYTES:
        raise QualitySyncError(f"下载文件过小: {size} bytes")
    return size


def mirror_local(
    bucket: oss2.Bucket,
    drama_id: str,
    *,
    dry_run: bool,
    force_local: bool,
) -> dict[str, Any]:
    target_dir = (
        AUTO_CUT_ROOT / f"#{drama_id}#" / "source" / QUALITY_DIR
    )
    summary = {
        "downloaded": 0,
        "existing": 0,
        "planned": 0,
        "failed": 0,
        "errors": [],
    }
    for item in list_quality_objects(bucket, drama_id):
        local = target_dir / Path(item["key"]).name
        if (
            not force_local
            and local.is_file()
            and local.stat().st_size == item["size"]
        ):
            summary["existing"] += 1
            continue
        if dry_run:
            summary["planned"] += 1
            continue
        try:
            local.parent.mkdir(parents=True, exist_ok=True)
            bucket.get_object_to_file(item["key"], str(local))
            if local.stat().st_size != item["size"]:
                raise QualitySyncError("本地文件大小与 OSS 不一致")
            summary["downloaded"] += 1
        except Exception as exc:
            summary["failed"] += 1
            summary["errors"].append(f"{Path(item['key']).name}: {exc}")
    return summary


def sync_drama(
    bucket: oss2.Bucket,
    cf_session: requests.Session | None,
    cf_token: str | None,
    drama_id: str,
    candidates: list[dict[str, Any]],
    *,
    dry_run: bool,
    force_local: bool,
) -> dict[str, Any]:
    manifest = load_manifest(bucket, drama_id)
    objects = list_quality_objects(bucket, drama_id)
    system_materials = (
        cf_materials(cf_session, cf_token, drama_id)
        if candidates and cf_session is not None and cf_token
        else []
    )
    summary: dict[str, Any] = {
        "drama_id": drama_id,
        "discovered": len(candidates),
        "matched": 0,
        "uploaded": 0,
        "oss_existing": 0,
        "planned": 0,
        "not_found": 0,
        "failed": 0,
        "errors": [],
    }
    manifest_changed = False

    for candidate in candidates:
        ad_name = str(candidate.get("ad_name") or "")
        material = match_cf_material(drama_id, ad_name, system_materials)
        if not material:
            summary["not_found"] += 1
            continue
        summary["matched"] += 1
        try:
            filename = safe_mp4_name(str(material.get("file_name") or ""))
            aliases = material_aliases(ad_name, filename, material)
            existing_key = manifest_existing_key(bucket, manifest, aliases)
            if not existing_key:
                existing_key = filename_existing_key(filename, objects)
            if existing_key:
                record_aliases(
                    manifest,
                    aliases,
                    {
                        "source_file_name": filename,
                        "ad_name": ad_name,
                        "oss_key": existing_key,
                        "status": "existing",
                        "synced_at": now_iso(),
                    },
                )
                manifest_changed = True
                summary["oss_existing"] += 1
                continue

            if dry_run:
                summary["planned"] += 1
                continue

            with tempfile.TemporaryDirectory(
                prefix=f"drama-quality-{drama_id}-"
            ) as temp_dir:
                path = Path(temp_dir) / filename
                size = download_source(
                    cf_session,
                    str(material.get("oss_url") or ""),
                    path,
                )
                digest = sha256_file(path)
                hash_key = hash_existing_key(bucket, manifest, digest)
                if hash_key:
                    record_aliases(
                        manifest,
                        aliases,
                        {
                            "source_file_name": filename,
                            "ad_name": ad_name,
                            "oss_key": hash_key,
                            "size": size,
                            "sha256": digest,
                            "status": "existing_by_hash",
                            "synced_at": now_iso(),
                        },
                    )
                    manifest_changed = True
                    summary["oss_existing"] += 1
                    continue

                oss_key = f"{quality_prefix(drama_id)}{filename}"
                if bucket.object_exists(oss_key):
                    summary["oss_existing"] += 1
                else:
                    result = bucket.put_object_from_file(
                        oss_key,
                        str(path),
                        headers={
                            "Content-Type": "video/mp4",
                            "x-oss-meta-sha256": digest,
                        },
                    )
                    if result.status not in (200, 201):
                        raise QualitySyncError(
                            f"OSS 上传失败: status={result.status}"
                        )
                    summary["uploaded"] += 1
                    objects.append({"key": oss_key, "size": size})

                record_aliases(
                    manifest,
                    aliases,
                    {
                        "source_file_name": filename,
                        "ad_name": ad_name,
                        "oss_key": oss_key,
                        "size": size,
                        "sha256": digest,
                        "status": "uploaded",
                        "synced_at": now_iso(),
                    },
                )
                manifest_changed = True
        except Exception as exc:
            summary["failed"] += 1
            summary["errors"].append(f"{ad_name[:80]}: {exc}")

    if manifest_changed and not dry_run:
        save_manifest(bucket, drama_id, manifest)
    summary["local"] = mirror_local(
        bucket,
        drama_id,
        dry_run=dry_run,
        force_local=force_local,
    )
    return summary


def summarize(items: list[dict[str, Any]], dry_run: bool) -> dict[str, Any]:
    totals = {
        "discovered": 0,
        "matched": 0,
        "uploaded": 0,
        "oss_existing": 0,
        "planned": 0,
        "not_found": 0,
        "failed": 0,
        "local_downloaded": 0,
        "local_existing": 0,
        "local_planned": 0,
        "local_failed": 0,
    }
    for item in items:
        for key in (
            "discovered",
            "matched",
            "uploaded",
            "oss_existing",
            "planned",
            "not_found",
            "failed",
        ):
            totals[key] += int(item.get(key) or 0)
        local = item.get("local") or {}
        totals["local_downloaded"] += int(local.get("downloaded") or 0)
        totals["local_existing"] += int(local.get("existing") or 0)
        totals["local_planned"] += int(local.get("planned") or 0)
        totals["local_failed"] += int(local.get("failed") or 0)
    return {
        "status": (
            "error"
            if totals["failed"] or totals["local_failed"]
            else "ok"
        ),
        "dry_run": dry_run,
        **totals,
        "items": items,
    }


def run(args: argparse.Namespace) -> int:
    series_ids, skipped_ids = parse_series_ids(args.series_ids)
    already_completed_ids: list[str] = []
    # ``--force-quality`` bypasses the source-completion ledger so that a
    # quality-only refresh can run for historical dramas, while still keeping
    # local files idempotent (unlike ``--force-local``).
    if not args.force_local and not args.force_quality:
        source_ledger = load_source_ledger()
        pending_ids: list[str] = []
        for drama_id in series_ids:
            if source_is_completed(drama_id, source_ledger):
                already_completed_ids.append(drama_id)
            else:
                pending_ids.append(drama_id)
        series_ids = pending_ids
    if not series_ids:
        payload = summarize([], args.dry_run)
        add_skip_summary(payload, skipped_ids, already_completed_ids)
        print(json.dumps(payload, ensure_ascii=False))
        return 0

    try:
        metabase_config = load_env(METABASE_ENV)
        oss_config = load_env(OSS_ENV)
        bucket = make_bucket(oss_config)
        mb_token = metabase_session(metabase_config)
        quality_rows = get_quality_materials(
            metabase_config,
            mb_token,
            series_ids,
        )
        by_drama: dict[str, list[dict[str, Any]]] = {
            drama_id: [] for drama_id in series_ids
        }
        for row in quality_rows:
            drama_id = str(row.get("series_id") or "")
            if drama_id in by_drama:
                by_drama[drama_id].append(row)

        cf_session: requests.Session | None = None
        cf_token: str | None = None
        if quality_rows:
            cf_config = load_env(CF_ENV)
            cf_session, cf_token = cf_login(cf_config)

        results: list[dict[str, Any]] = []
        for drama_id in series_ids:
            log(
                f"#{drama_id}# 发现 {len(by_drama[drama_id])} 条好素材，"
                "开始 OSS 去重与本地镜像"
            )
            results.append(
                sync_drama(
                    bucket,
                    cf_session,
                    cf_token,
                    drama_id,
                    by_drama[drama_id],
                    dry_run=args.dry_run,
                    force_local=args.force_local,
                )
            )

        payload = summarize(results, args.dry_run)
        add_skip_summary(payload, skipped_ids, already_completed_ids)
        print(json.dumps(payload, ensure_ascii=False))
        return 1 if payload["status"] == "error" else 0
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "error",
                    "dry_run": args.dry_run,
                    "error": str(exc),
                },
                ensure_ascii=False,
            )
        )
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="drama-material-hub 内置优质素材同步"
    )
    parser.add_argument(
        "--series-ids",
        required=True,
        help="逗号分隔的剧ID",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只查询和规划，不写 OSS 或本地",
    )
    parser.add_argument(
        "--force-local",
        action="store_true",
        help="强制从 OSS 重新下载本地优质素材",
    )
    parser.add_argument(
        "--force-quality",
        action="store_true",
        help="忽略 source 完成记录，重新检查优质素材；本地已有文件不重下",
    )
    parser.add_argument(
        "--parent-ledger-lock",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args()
    if args.parent_ledger_lock:
        return run(args)
    with source_ledger_lock():
        return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
