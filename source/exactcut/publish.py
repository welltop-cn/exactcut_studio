from __future__ import annotations

import json
import mimetypes
import os
import posixpath
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

from .errors import PublishError
from .runtime import require_publish
from .naming import validate_material_filename
from .util import env, file_sha256, now_iso


GUARDED_DELIVERY = ContextVar('guarded_delivery', default=False)
DELIVERY_RECEIPT = ContextVar('delivery_receipt', default=None)


@contextmanager
def guarded_delivery(*, receipt=None):
    token = GUARDED_DELIVERY.set(True)
    receipt_token = DELIVERY_RECEIPT.set(receipt)
    try: yield
    finally:
        DELIVERY_RECEIPT.reset(receipt_token)
        GUARDED_DELIVERY.reset(token)


def delivery_receipt(value):
    observer = DELIVERY_RECEIPT.get()
    if observer is not None: observer(value)


FEISHU_API = "https://open.feishu.cn/open-apis"
STATUS_FIELD = {
    "field_name": "状态", "type": 3,
    "property": {"options": [
        {"name": "待处理", "color": 0}, {"name": "处理中", "color": 1},
        {"name": "同步中", "color": 4}, {"name": "已完成", "color": 2},
        {"name": "失败", "color": 3}, {"name": "超时", "color": 5}, {"name": "暂停", "color": 6},
    ]},
}
FEISHU_FIELD_DEFS = [
    {"field_name": "剧ID", "type": 1},
    {"field_name": "OSS_URL", "type": 15},
    {"field_name": "来源", "type": 1},
    {"field_name": "ADS素材ID", "type": 1},
    {"field_name": "视频ID", "type": 1},
    {"field_name": "封面URL", "type": 15},
    {"field_name": "处理批次ID", "type": 1},
    {"field_name": "处理开始时间", "type": 5, "property": {"date_formatter": "yyyy-MM-dd HH:mm", "auto_fill": False}},
    {"field_name": "下次执行时间", "type": 5, "property": {"date_formatter": "yyyy-MM-dd HH:mm", "auto_fill": False}},
    {"field_name": "重试次数", "type": 2},
    {"field_name": "错误信息", "type": 1},
    {"field_name": "最后查询时间", "type": 5, "property": {"date_formatter": "yyyy-MM-dd HH:mm", "auto_fill": False}},
    {"field_name": "完成时间", "type": 5, "property": {"date_formatter": "yyyy-MM-dd HH:mm", "auto_fill": False}},
]


def _response_json(response: requests.Response, action: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise PublishError(f"{action} 返回非JSON：HTTP {response.status_code}") from exc
    if not response.ok or payload.get("code", 0) != 0:
        raise PublishError(f"{action}失败：{json.dumps(payload, ensure_ascii=False)}")
    return payload


class OssPublisher:
    def __init__(self) -> None:
        self.bucket_name = str(env("ALIYUN_OSS_BUCKET", required=False, default="local-dry-run"))
        self.endpoint = str(env("ALIYUN_OSS_ENDPOINT", required=False, default="https://example.invalid"))
        self.prefix = str(env("ALIYUN_OSS_PREFIX", required=False, default="fb-bm-assets"))
        self.public_base = env("ALIYUN_OSS_PUBLIC_BASE_URL", required=False)
        self.bucket = None

    def connect(self):
        if self.bucket is not None:
            return self.bucket
        require_publish()
        try:
            import oss2
        except ImportError as exc:
            raise PublishError("缺少oss2依赖") from exc
        access_id = env("ALIYUN_OSS_ACCESS_KEY_ID")
        secret = env("ALIYUN_OSS_ACCESS_KEY_SECRET")
        token = env("ALIYUN_SECURITY_TOKEN", required=False)
        auth = oss2.StsAuth(access_id, secret, token) if token else oss2.Auth(access_id, secret)
        self.bucket = oss2.Bucket(auth, self.endpoint, self.bucket_name)
        return self.bucket

    def key_for(self, video: Path) -> tuple[str, dict[str, str]]:
        parsed = validate_material_filename(video.name)
        return posixpath.join(self.prefix.strip("/"), parsed["novel_id"], video.name), parsed

    def public_url(self, key: str) -> str:
        encoded = "/".join(quote(part, safe="") for part in key.split("/"))
        if self.public_base:
            return f"{str(self.public_base).rstrip('/')}/{encoded}"
        host = self.endpoint.replace("https://", "").replace("http://", "").rstrip("/")
        scheme = "https" if self.endpoint.startswith("https://") else "http"
        return f"{scheme}://{self.bucket_name}.{host}/{encoded}"

    def upload(self, video: Path, *, dry_run: bool = False) -> dict[str, Any]:
        key, parsed = self.key_for(video)
        digest = file_sha256(video)
        existed = False
        if not dry_run:
            bucket = self.connect()
            existed = bucket.object_exists(key)
            if existed:
                meta = bucket.get_object_meta(key)
                remote_hash = meta.headers.get("x-oss-meta-sha256") or meta.headers.get("X-Oss-Meta-Sha256")
                if not remote_hash:
                    raise PublishError(f"OSS已有同名对象但缺少SHA-256元数据，禁止复用：{key}")
                if remote_hash != digest:
                    raise PublishError(f"OSS同名对象内容不同，必须更换素材序号：{key}")
            else:
                content_type = mimetypes.guess_type(video.name)[0] or "video/mp4"
                result = bucket.put_object_from_file(
                    key, str(video), headers={"Content-Type": content_type, "x-oss-meta-sha256": digest}
                )
                if result.status not in (200, 201):
                    raise PublishError(f"OSS上传失败：status={result.status}")
            bucket.put_object_acl(key, "public-read")
            if GUARDED_DELIVERY.get():
                meta = bucket.get_object_meta(key)
                remote_hash = meta.headers.get("x-oss-meta-sha256") or meta.headers.get("X-Oss-Meta-Sha256")
                if remote_hash != digest:
                    raise PublishError(f"OSS上传后校验失败，待核实：{key}")
        return {
            **parsed, "oss_key": key, "oss_url": self.public_url(key), "oss_existed": existed,
            "sha256": digest, "size": video.stat().st_size, "dry_run": dry_run,
        }


class FeishuClient:
    def __init__(self) -> None:
        require_publish()
        self.app_token = str(env("FEISHU_BITABLE_APP_TOKEN"))
        self.table_id = str(env("FEISHU_BITABLE_TABLE_ID"))
        response = requests.post(
            f"{FEISHU_API}/auth/v3/tenant_access_token/internal",
            json={"app_id": env("FEISHU_APP_ID"), "app_secret": env("FEISHU_APP_SECRET")}, timeout=30,
        )
        payload = _response_json(response, "飞书鉴权")
        self.token = str(payload.get("tenant_access_token") or payload.get("data", {}).get("tenant_access_token") or "")
        if not self.token:
            raise PublishError("飞书鉴权没有返回tenant_access_token")

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

    def _url(self, suffix: str = "") -> str:
        base = f"{FEISHU_API}/bitable/v1/apps/{self.app_token}/tables/{self.table_id}/records"
        return f"{base}/{suffix}" if suffix else base

    def fields(self) -> dict[str, dict[str, Any]]:
        response = requests.get(
            f"{FEISHU_API}/bitable/v1/apps/{self.app_token}/tables/{self.table_id}/fields",
            headers=self.headers, params={"page_size": 500}, timeout=30,
        )
        items = _response_json(response, "读取飞书字段").get("data", {}).get("items") or []
        return {str(item.get("field_name")): item for item in items}

    def ensure_schema(self, *, dry_run: bool = False) -> dict[str, Any]:
        existing = self.fields()
        created: list[str] = []
        updated: list[str] = []
        skipped: list[str] = []
        fields_url = f"{FEISHU_API}/bitable/v1/apps/{self.app_token}/tables/{self.table_id}/fields"
        status = existing.get("状态")
        desired_names = [item["name"] for item in STATUS_FIELD["property"]["options"]]
        current_names = [item.get("name") for item in (status or {}).get("property", {}).get("options", [])]
        if not status:
            if not dry_run:
                _response_json(requests.post(fields_url, headers=self.headers, json=STATUS_FIELD, timeout=30), "创建状态字段")
            created.append("状态")
        elif status.get("type") != 3 or current_names != desired_names:
            desired = json.loads(json.dumps(STATUS_FIELD, ensure_ascii=False))
            old_by_name = {item.get("name"): item for item in (status.get("property", {}).get("options") or [])}
            for option in desired["property"]["options"]:
                if old_by_name.get(option["name"], {}).get("id"):
                    option["id"] = old_by_name[option["name"]]["id"]
            if not dry_run:
                _response_json(
                    requests.put(f"{fields_url}/{status['field_id']}", headers=self.headers, json=desired, timeout=30),
                    "更新状态字段",
                )
            updated.append("状态")
        else:
            skipped.append("状态")
        for definition in FEISHU_FIELD_DEFS:
            name = str(definition["field_name"])
            if name in existing:
                skipped.append(name)
                continue
            if not dry_run:
                _response_json(requests.post(fields_url, headers=self.headers, json=definition, timeout=30), f"创建字段{name}")
            created.append(name)
        return {"dry_run": dry_run, "created": created, "updated": updated, "skipped_existing": skipped}

    def search_by_url(self, oss_url: str) -> dict[str, Any] | None:
        body = {"filter": {"conjunction": "and", "conditions": [{"field_name": "OSS_URL", "operator": "is", "value": [oss_url]}]}, "page_size": 20}
        response = requests.post(f"{self._url()}/search", headers=self.headers, json=body, timeout=30)
        items = _response_json(response, "搜索飞书记录").get("data", {}).get("items") or []
        if GUARDED_DELIVERY.get() and len(items) > 1:
            raise PublishError("OSS_URL 对应多条飞书记录，待核实")
        return items[0] if items else None

    def create_or_reuse(self, fields: dict[str, Any]) -> dict[str, Any]:
        existing = self.search_by_url(fields["OSS_URL"]["link"])
        if existing:
            return {"record_id": existing.get("record_id") or existing.get("id"), "action": "reused", "fields": existing.get("fields") or {}}
        schema = self.fields()
        required = {"剧ID", "OSS_URL", "状态", "来源", "重试次数", "ADS素材ID", "视频ID", "封面URL"}
        missing = sorted(required - set(schema))
        if missing:
            raise PublishError(f"飞书表缺少必要字段：{missing}；请先运行 exactcut ensure-feishu --dry-run")
        filtered = {key: value for key, value in fields.items() if key in schema and not schema[key].get("is_primary")}
        response = requests.post(self._url(), headers=self.headers, json={"fields": filtered}, timeout=30)
        record = _response_json(response, "创建飞书记录").get("data", {}).get("record") or {}
        return {"record_id": record.get("record_id") or record.get("id"), "action": "created", "fields": filtered}

    def get_record(self, record_id: str) -> dict[str, Any]:
        response = requests.get(self._url(record_id), headers=self.headers, timeout=30)
        return _response_json(response, "读取飞书记录").get("data", {}).get("record") or {}

    def update(self, record_id: str, fields: dict[str, Any]) -> None:
        schema = self.fields()
        filtered = {key: value for key, value in fields.items() if key in schema and not schema[key].get("is_primary")}
        response = requests.put(self._url(record_id), headers=self.headers, json={"fields": filtered}, timeout=30)
        _response_json(response, "更新飞书记录")


class CfAdsClient:
    def __init__(self) -> None:
        require_publish()
        self.base = str(env("CF_ADS_BASE_URL")).rstrip("/")
        self.username = str(env("CF_ADS_USERNAME"))
        self.password = str(env("CF_ADS_PASSWORD"))
        self.timeout = int(env("HTTP_TIMEOUT_SECONDS", required=False, default="30") or 30)
        self.token = ""

    def request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        guarded = GUARDED_DELIVERY.get()
        attempts = 2 if guarded else 4
        custom_headers = kwargs.pop('headers', {})
        for attempt in range(attempts):
            if not self.token and path != "/api/v1/auth/login":
                self.login()
            headers = dict(custom_headers)
            if self.token:
                headers = {**headers, "Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
            response = requests.request(method, f"{self.base}{path}", headers=headers, timeout=self.timeout, **kwargs)
            try:
                payload = response.json()
            except ValueError as exc:
                raise PublishError(f"CF-Ads返回非JSON：HTTP {response.status_code}") from exc
            if payload.get("code") in (100006, 100009) and attempt == 0:
                self.token = ""
                self.login()
                continue
            if payload.get("code") not in (200, 200005, 200008) and not guarded and attempt < attempts - 1:
                time.sleep(2**attempt)
                continue
            return payload
        return payload

    def login(self) -> None:
        response = requests.post(
            f"{self.base}/api/v1/auth/login", json={"username": self.username, "password": self.password}, timeout=self.timeout
        )
        payload = response.json()
        if payload.get("code") != 200:
            raise PublishError(f"CF-Ads登录失败：{json.dumps(payload, ensure_ascii=False)}")
        self.token = str(payload["data"]["token"])

    def app_usage(self) -> dict[str, Any]:
        payload = self.request("GET", "/api/v1/facebook/app_usage")
        if payload.get("code") != 200:
            raise PublishError(f"CF-Ads额度查询失败：{json.dumps(payload, ensure_ascii=False)}")
        return payload.get("data") or {}

    def ingest(self, video_url: str) -> str:
        payload = self.request("POST", "/api/v1/material/ingest_and_sync", json={"video_url": video_url, "platforms": ["facebook"], "wait_until_ready": False})
        data = payload.get("data") or {}
        material_id = data.get("material_id") or (data.get("material") or {}).get("id")
        if payload.get("code") not in (200, 200005, 200008) or not material_id:
            raise PublishError(f"CF-Ads导入失败：{json.dumps(payload, ensure_ascii=False)}")
        return str(material_id)

    def sync_status(self, material_id: str) -> dict[str, Any]:
        payload = self.request("GET", f"/api/v1/material/{material_id}/sync_status")
        if payload.get("code") != 200:
            raise PublishError(f"CF-Ads状态查询失败：{json.dumps(payload, ensure_ascii=False)}")
        return payload.get("data", {}).get("material") or {}

    def resync(self, material_id: str) -> None:
        payload = self.request("POST", "/api/v1/material/sync/facebook", json={"material_ids": [int(material_id)]})
        if payload.get("code") != 200:
            raise PublishError(f"CF-Ads重试失败：{json.dumps(payload, ensure_ascii=False)}")


def feishu_url(url: str, text: str | None = None) -> dict[str, str]:
    return {"link": url, "text": text or url}


def publish_to_queue(video: Path, manifest: Any, request: Any, *, dry_run: bool = False) -> dict[str, Any]:
    upload = OssPublisher().upload(video, dry_run=dry_run)
    if dry_run:
        return {"stage": "dry_run", **upload, "feishu_record_id": None}
    source = os.getenv("OPENCLAW_SOURCE_USER") or manifest.author
    fields = {
        "剧ID": manifest.drama_id,
        "OSS_URL": feishu_url(upload["oss_url"]),
        "状态": "待处理",
        "来源": source,
        "重试次数": 0,
        "剧集ID": manifest.drama_id,
        "序号": request.seq,
        "业务日期": f"{manifest.business_date[:4]}-{manifest.business_date[4:6]}-{manifest.business_date[6:]}",
        "OSS_KEY": upload["oss_key"],
    }
    record = FeishuClient().create_or_reuse(fields)
    delivery_receipt({"stage": "queued", **upload, "feishu_record_id": record["record_id"], "feishu_action": record["action"]})
    if GUARDED_DELIVERY.get():
        remote = FeishuClient().get_record(str(record["record_id"]))
        value = (remote.get("fields") or {}).get("OSS_URL")
        remote_url = value.get("link") if isinstance(value, dict) else str(value or "")
        if remote_url != upload["oss_url"]:
            raise PublishError("飞书记录上传后目标核验失败，待核实")
    return {"stage": "queued", **upload, "feishu_record_id": record["record_id"], "feishu_action": record["action"]}


def verify_sync_fields(feishu, record_id, expected):
    """Only the insured path requires remote evidence after filtered updates."""
    if not GUARDED_DELIVERY.get(): return
    record = feishu.get_record(record_id)
    if str(record.get("record_id") or record.get("id") or "") != record_id:
        raise PublishError("飞书同步写入后记录身份核验失败，待核实")
    fields = record.get("fields") or {}
    for name, value in expected.items():
        actual = fields.get(name)
        if name == "重试次数":
            try: matched = actual is not None and int(actual) == int(value)
            except (TypeError, ValueError): matched = False
        else: matched = actual is not None and str(actual) == str(value)
        if not matched:
            raise PublishError(f"飞书同步写入后字段核验失败，待核实：{name}")


def sync_record_once(record_id: str) -> dict[str, Any]:
    feishu = FeishuClient()
    cf = CfAdsClient()
    usage = cf.app_usage()
    percent = max(float(usage.get("call_count") or 0), float(usage.get("total_time") or 0))
    if not usage.get("is_available") or percent >= 85:
        feishu.update(record_id, {"状态": "同步中", "错误信息": "Meta API额度接近上限，自动延后", "下次执行时间": int(time.time() * 1000) + 600000})
        verify_sync_fields(feishu, record_id, {"状态": "同步中"})
        return {"record_id": record_id, "status": "quota_deferred", "usage": usage}
    record = feishu.get_record(record_id)
    fields = record.get("fields") or {}
    url_value = fields.get("OSS_URL")
    oss_url = url_value.get("link") if isinstance(url_value, dict) else str(url_value or "")
    material_id = str(fields.get("ADS素材ID") or "").strip()
    retries = int(fields.get("重试次数") or 0)
    max_retries = int(os.getenv("MAX_RETRIES", "2"))
    if material_id: delivery_receipt({"record_id": record_id, "material_id": material_id})
    feishu.update(record_id, {"状态": "处理中", "处理开始时间": int(time.time() * 1000)})
    if not material_id:
        material_id = cf.ingest(oss_url)
        delivery_receipt({"record_id": record_id, "material_id": material_id})
        feishu.update(record_id, {"ADS素材ID": material_id, "状态": "同步中", "错误信息": ""})
        verify_sync_fields(feishu, record_id, {"ADS素材ID": material_id, "状态": "同步中"})
    material = cf.sync_status(material_id)
    fb_status = str(material.get("facebook_bm_sync_status") or "none")
    video_id = material.get("facebook_bm_video_id")
    thumbnail = material.get("facebook_thumbnail_url")
    patch: dict[str, Any] = {"ADS素材ID": material_id, "最后查询时间": int(time.time() * 1000), "错误信息": material.get("facebook_remark") or ""}
    if video_id:
        patch["视频ID"] = str(video_id)
    if thumbnail:
        patch["封面URL"] = feishu_url(str(thumbnail), "cover")
    if fb_status == "ready" and video_id:
        patch.update({"状态": "已完成", "完成时间": int(time.time() * 1000), "下次执行时间": None})
        action = "ready"
    elif fb_status in {"failed", "none", ""} and retries < max_retries:
        cf.resync(material_id)
        retries += 1
        delivery_receipt({"record_id": record_id, "material_id": material_id, "fields": {"重试次数": retries}})
        patch.update({"状态": "同步中", "重试次数": retries, "下次执行时间": int(time.time() * 1000) + int(os.getenv("NEXT_POLL_SECONDS", "60")) * 1000})
        action = "resync_requested"
    elif retries >= max_retries:
        patch["状态"] = "失败"
        action = "failed"
    else:
        patch.update({"状态": "同步中", "下次执行时间": int(time.time() * 1000) + int(os.getenv("NEXT_POLL_SECONDS", "60")) * 1000})
        action = "syncing"
    result = {"record_id": record_id, "action": action, "fb_status": fb_status, "material_id": material_id, "video_id": video_id, "thumbnail": thumbnail}
    delivery_receipt(result)
    feishu.update(record_id, patch)
    expected = {"ADS素材ID": material_id, "重试次数": retries, "状态": patch["状态"]}
    if video_id: expected["视频ID"] = str(video_id)
    verify_sync_fields(feishu, record_id, expected)
    return result


def verify_record_once(binding):
    """Read only the saved record/material: no ingest, resync, or Feishu writes."""
    record_id, material_id = binding['record_id'], binding['material_id']
    record = FeishuClient().get_record(record_id)
    fields = record.get('fields') or {}
    url_value = fields.get('OSS_URL')
    oss_url = url_value.get('link') if isinstance(url_value, dict) else str(url_value or '')
    remote_material = str(fields.get('ADS素材ID') or '').strip()
    if (str(record.get('record_id') or record.get('id') or '') != record_id
            or oss_url != binding['oss_url'] or remote_material != material_id):
        raise PublishError('保存回执与飞书原记录身份、URL 或素材 ID 不一致，待核实')
    material = CfAdsClient().sync_status(material_id)
    status = str(material.get('facebook_bm_sync_status') or 'none')
    video = material.get('facebook_bm_video_id')
    return {'record_id': record_id, 'material_id': material_id, 'fb_status': status,
            'video_id': video, 'thumbnail': material.get('facebook_thumbnail_url'),
            'action': 'ready' if status == 'ready' and video else 'failed' if status in {'failed', 'none', ''} else 'syncing'}


def wait_until_ready(record_id: str, config: dict[str, Any]) -> dict[str, Any]:
    deadline = time.monotonic() + int(config["publish"]["wait_timeout_seconds"])
    poll = int(config["publish"]["poll_seconds"])
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = sync_record_once(record_id)
        if last.get("action") == "ready":
            return last
        if last.get("action") == "failed":
            raise PublishError(f"FBBM同步失败：{last}")
        time.sleep(max(1, poll))
    raise PublishError(f"等待FBBM就绪超时，最后状态：{last}")
