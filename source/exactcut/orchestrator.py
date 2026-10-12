from __future__ import annotations

import json
import os
import shutil
import traceback
import time
from pathlib import Path
from typing import Any

from . import result_store
from . import diagnostics as diag
from .compiler import compile_workbook
from .errors import ExactCutError
from .media import resolve_episode_video, validate_manifest_sources, resolve_full_episodes
from .models import BatchManifest
from .publish import publish_to_queue, wait_until_ready
from .qc import inspect_output
from .renderer import render_request
from .preview import make_preview, available_preview
from .preview_oss import upload_generated_preview, upload_generated_original
from .util import file_sha256, json_sha256, now_iso, read_json, write_json_atomic


def load_manifest(path: Path) -> BatchManifest:
    return BatchManifest.model_validate(read_json(path))


def compile_batch(project_dir: Path, workbook: Path, config: dict[str, Any]) -> tuple[BatchManifest, Path]:
    temporary = compile_workbook(workbook, config)
    manifest_path = project_dir / "requests" / f"{temporary.business_date}.manifest.json"
    manifest = compile_workbook(workbook, config, manifest_path)
    return manifest, manifest_path


def _request_fingerprint(project_dir: Path, request: Any, config: dict[str, Any], source_cache: dict | None = None) -> str:
    sources = []
    for item in request.timeline:
        if item.type == "episode":
            source = resolve_episode_video(project_dir, str(item.episode))
            stat = source.stat()
            identity = (stat.st_ino, stat.st_size, stat.st_mtime_ns)
            cached = source_cache.get(str(source)) if source_cache is not None else None
            if cached and cached['identity'] == identity:
                digest = cached['sha256']
            else:
                digest = file_sha256(source)
                if source_cache is not None:
                    source_cache[str(source)] = {'identity': identity, 'sha256': digest}
            sources.append({"path": str(source), "sha256": digest})
    ending = getattr(request, 'ending', None)
    if ending is not None and ending.mode == 'image':
        from .ending import image_file
        image = image_file(project_dir, ending)
        sources.append({'path': str(image), 'sha256': file_sha256(image)})
    # Operational log retention does not change media bytes or cache identity.
    media_config = {key: value for key, value in config.items() if key not in {'logging', 'preview'}}
    return json_sha256({"pipeline": "exact_timeline_v3_unit_qc", "request": request.model_dump(mode="json"), "sources": sources, "config": media_config})


def _new_results(manifest: BatchManifest) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "drama_id": manifest.drama_id,
        "business_date": manifest.business_date,
        "author": manifest.author,
        "updated_at": now_iso(),
        "requests": {},
    }


def execute_batch(
    project_dir: Path,
    manifest: BatchManifest,
    config: dict[str, Any],
    *,
    request_ids: list[str] | None = None,
    force: bool = False,
    publish: bool = False,
    wait_ready: bool = False,
    publish_dry_run: bool = False,
    output_dir: Path | None = None,
    progress: Any = None,
) -> dict[str, Any]:
    project_dir = project_dir.resolve()
    request_ids = request_ids or None  # Preserve the legacy empty-selection convention.
    batch_started = time.monotonic()
    source_cache = {}
    if request_ids is not None:
        unknown = set(request_ids) - {r.request_id for r in manifest.requests}
        if unknown: raise ExactCutError(f"未知需求编号：{sorted(unknown)}")
        manifest = manifest.model_copy(update={'requests': [r for r in manifest.requests if r.request_id in request_ids]})
    manifest = resolve_full_episodes(manifest, project_dir)
    validate_manifest_sources(manifest, project_dir)
    batch_dir = output_dir or project_dir / "outputs" / manifest.business_date
    for request in manifest.requests:
        result_store.file_for(batch_dir, request.request_id)
    videos_dir = batch_dir / "videos"
    staging_dir = batch_dir / ".staging"
    failed_dir = batch_dir / "failed"
    audit_dir = batch_dir / "audit"
    for directory in (videos_dir, staging_dir, failed_dir, audit_dir):
        directory.mkdir(parents=True, exist_ok=True)
    resolved = batch_dir / ('resolved.manifest.json' if request_ids is None else f'resolved-{manifest.requests[0].request_id}.manifest.json')
    write_json_atomic(resolved, manifest.model_dump(mode='json'))
    results_path = batch_dir / "results.json"
    results = {**_new_results(manifest), **result_store.read(batch_dir)}
    def persist():
        if request_ids is None:
            write_json_atomic(results_path, results)
        for rid, record in results["requests"].items():
            if rid in selected and (request_ids is not None or result_store.file_for(batch_dir, rid).exists()):
                result_store.put(batch_dir, rid, record)
    selected = set(request_ids or [request.request_id for request in manifest.requests])
    unknown = selected - {request.request_id for request in manifest.requests}
    if unknown:
        raise ExactCutError(f"未知需求编号：{sorted(unknown)}")

    for request in manifest.requests:
        if request.request_id not in selected:
            continue
        fingerprint = _request_fingerprint(project_dir, request, config, source_cache)
        output = videos_dir / request.output_filename
        previous = results["requests"].get(request.request_id) or {}
        reusable_output = (
            not force
            and previous.get("fingerprint") == fingerprint
            and output.exists()
            and previous.get("output_sha256") == file_sha256(output)
            and previous.get("status") in {"qc_passed", "publish_dry_run", "publish_failed", "fbbm_queued", "fbbm_ready"}
        )
        if reusable_output:
            diag.event('render.output_reused', request_id=request.request_id, output_sha256=previous.get('output_sha256'))
            if not available_preview(batch_dir.parent, request, previous) and output_dir is not None:
                try:
                    previous['preview'] = make_preview(output, batch_dir / 'previews' / request.output_filename, previous['output_sha256'])
                except Exception as exc:
                    previous['preview_error'] = str(exc)
                persist()
            upload_generated_preview(batch_dir, request, previous)
            upload_generated_original(batch_dir, request, previous)
            persist()
            previous["cache_reused"] = True
            if progress:
                progress('reused', request.request_id)
            if not publish or previous.get("status") == "fbbm_ready":
                continue
            try:
                publication = previous.get("publication") or {}
                record_id = publication.get("feishu_record_id")
                if not record_id or publish_dry_run:
                    publication = publish_to_queue(output, manifest, request, dry_run=publish_dry_run)
                    previous["publication"] = publication
                    record_id = publication.get("feishu_record_id")
                previous["status"] = "publish_dry_run" if publish_dry_run else "fbbm_queued"
                if wait_ready and not publish_dry_run:
                    previous["fbbm"] = wait_until_ready(str(record_id), config)
                    previous["status"] = "fbbm_ready"
                previous.pop("error", None)
                previous.pop("error_type", None)
            except Exception as exc:
                previous.update({"status": "publish_failed", "error": str(exc), "error_type": type(exc).__name__, "failed_at": now_iso()})
            results["updated_at"] = now_iso()
            persist()
            continue
        record: dict[str, Any] = {
            "request_id": request.request_id,
            "output_filename": request.output_filename,
            "fingerprint": fingerprint,
            "status": "processing",
            "started_at": now_iso(),
            "cache_reused": False,
        }
        results["requests"][request.request_id] = record
        results["updated_at"] = now_iso()
        persist()
        staged = staging_dir / request.output_filename
        request_audit = audit_dir / request.request_id
        request_audit.mkdir(parents=True, exist_ok=True)
        def stage(name: str, **details) -> None:
            if record.get('stage') != name:
                record['stage_started_at'] = now_iso()
            record['stage'] = name
            record['progress_details'] = details
            record['last_progress_at'] = now_iso()
            results['updated_at'] = now_iso()
            persist()
            if progress:
                progress(name, request.request_id)
        try:
            request_started = time.monotonic()
            timeline = [item.model_dump(mode="json") for item in request.timeline]
            stage('rendering')
            render_plan = render_request(project_dir, request, timeline, staged, config, source_cache=source_cache, progress=stage)
            record['hook_emphasis'] = (render_plan.get('hook_visual') or {}).get('emphasis')
            record['planned_duration_ms'] = render_plan['expected_duration_ms']
            record['unit_checks'] = render_plan.get('unit_checks', [])
            record['stage_seconds'] = render_plan.get('stage_seconds', {})
            record['base_cache_reused'] = render_plan.get('base_cache_reused', False)
            visual = render_plan.get('hook_visual') or {}
            record['hook_warnings'] = [message for message in (
                (visual.get('placement') or {}).get('warning'), visual.get('readability_warning'),
                (visual.get('sampling') or {}).get('warning')) if message]
            write_json_atomic(request_audit / "clip_plan.json", {
                "request": request.model_dump(mode="json"), **render_plan,
            })
            stage('qc')
            stage_started = time.monotonic()
            qc = inspect_output(staged, int(render_plan["expected_duration_ms"]), config, timeline)
            diag.event('render.qc', level='info' if qc['allowed'] else 'error', request_id=request.request_id,
                       expected_ms=qc.get('expected_duration_ms'), actual_ms=qc.get('actual_duration_ms'),
                       allowed=qc['allowed'], errors=qc.get('errors'))
            record['stage_seconds']['qc'] = round(time.monotonic()-stage_started,3)
            write_json_atomic(request_audit / "render_qc.json", qc)
            if not qc["allowed"]:
                raise ExactCutError(f"成片QC未通过：{qc['errors']}")
            os.replace(staged, output)
            record.update({
                "status": "qc_passed",
                "completed_at": now_iso(),
                "output": str(output),
                "output_sha256": file_sha256(output),
                "duration_ms": qc["actual_duration_ms"],
                "qc": qc,
            })
            try:
                stage('preview')
                stage_started = time.monotonic()
                record['preview'] = make_preview(output, batch_dir / 'previews' / request.output_filename, record['output_sha256'])
                record['stage_seconds']['preview'] = round(time.monotonic()-stage_started,3)
            except Exception as exc:
                record['preview_error'] = str(exc)
            upload_generated_preview(batch_dir, request, record)
            upload_generated_original(batch_dir, request, record)
            if publish:
                publication = publish_to_queue(output, manifest, request, dry_run=publish_dry_run)
                record["publication"] = publication
                record["status"] = "publish_dry_run" if publish_dry_run else "fbbm_queued"
                if wait_ready and not publish_dry_run:
                    ready = wait_until_ready(str(publication["feishu_record_id"]), config)
                    record["fbbm"] = ready
                    record["status"] = "fbbm_ready"
        except Exception as exc:
            from .stage_results import ResourceDeferred
            diag.event('render.request_failed', level='warning' if isinstance(exc, ResourceDeferred) else 'error',
                       request_id=request.request_id, stage=record.get('stage'), error=exc)
            if isinstance(exc, ResourceDeferred):
                record.update(status='waiting_resource', not_before=exc.not_before)
                raise
            if staged.exists():
                quarantine = failed_dir / f"{request.request_id}-{staged.name}"
                if quarantine.exists():
                    quarantine = failed_dir / f"{request.request_id}-{now_iso().replace(':', '')}-{staged.name}"
                shutil.move(str(staged), str(quarantine))
                record["quarantined_output"] = str(quarantine)
            record.update({
                "status": "publish_failed" if output.exists() and record.get("output_sha256") else "failed",
                "failed_at": now_iso(),
                "error": str(exc),
                "error_type": type(exc).__name__,
                "traceback": traceback.format_exc(limit=12),
            })
        finally:
            record['execution_ended_at'] = now_iso()
            record['elapsed_seconds'] = round(time.monotonic() - request_started, 3)
            results["updated_at"] = now_iso()
            persist()
            if progress:
                progress(record['status'], request.request_id)
    results['metrics'] = {'elapsed_seconds': round(time.monotonic() - batch_started, 3), 'unique_source_files_hashed': len(source_cache)}
    persist()
    return results
