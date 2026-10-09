from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .compiler import compile_workbook
from .errors import ExactCutError
from .media import validate_manifest_sources
from .orchestrator import compile_batch, execute_batch, load_manifest
from .prepare import prepare_project
from .publish import FeishuClient, sync_record_once
from .util import load_config, load_env_file


def _project(value: str) -> Path:
    from .runtime import protect_path
    protect_path(value)
    path = Path(value).expanduser().resolve()
    if not (path / "project.json").exists():
        raise argparse.ArgumentTypeError(f"项目目录缺少 project.json：{path}")
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="exactcut", description="表格驱动精确剪辑与FBBM交付")
    parser.add_argument("--env-file", type=Path, default=None)
    parser.add_argument("--config", type=Path, default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser("prepare", help="从EP*.mp4生成基础物理镜头索引")
    prepare.add_argument("--project", type=_project, required=True)

    compile_cmd = sub.add_parser("compile", help="把填写表格编译为不可变Manifest")
    compile_cmd.add_argument("--project", type=_project, required=True)
    compile_cmd.add_argument("--input", type=Path, required=True)

    validate = sub.add_parser("validate", help="校验表格、命名、素材、授权和时间范围")
    validate.add_argument("--project", type=_project, required=True)
    validate.add_argument("--input", type=Path)
    validate.add_argument("--manifest", type=Path)

    execute = sub.add_parser("execute", help="按表剪辑、格式与时长检查，并可上传FBBM")
    execute.add_argument("--project", type=_project, required=True)
    execute.add_argument("--input", type=Path)
    execute.add_argument("--manifest", type=Path)
    execute.add_argument("--request-id", action="append")
    execute.add_argument("--force", action="store_true")
    execute.add_argument("--publish", action="store_true")
    execute.add_argument("--publish-dry-run", action="store_true")
    execute.add_argument("--wait-ready", action="store_true")

    ensure = sub.add_parser("ensure-feishu", help="检查并补齐FBBM同步需要的飞书字段")
    ensure.add_argument("--dry-run", action="store_true")

    worker = sub.add_parser("sync-record", help="执行一次指定飞书记录的FBBM同步")
    worker.add_argument("--record-id", required=True)
    return parser


def _manifest_from_args(args: argparse.Namespace, config: dict) -> tuple[object, Path | None]:
    if args.input and args.manifest:
        raise ExactCutError("--input 和 --manifest 只能选一个")
    if args.input:
        return compile_batch(args.project, args.input, config)
    if args.manifest:
        return load_manifest(args.manifest.expanduser().resolve()), args.manifest
    raise ExactCutError("必须提供 --input 或 --manifest")


def main(argv: list[str] | None = None) -> int:
    from .stage_results import run_child
    return run_child(_main, argv)


def _main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        load_env_file(args.env_file.expanduser().resolve() if args.env_file else None)
        config = load_config(args.config.expanduser().resolve() if args.config else None)
        if args.command == "prepare":
            outputs = prepare_project(args.project)
            payload = {"status": "completed", "indexes": [str(item) for item in outputs]}
        elif args.command == "compile":
            manifest, path = compile_batch(args.project, args.input, config)
            payload = {"status": "compiled", "manifest": str(path), "requests": len(manifest.requests)}
        elif args.command == "validate":
            manifest, path = _manifest_from_args(args, config)
            sources = validate_manifest_sources(manifest, args.project)
            payload = {"status": "valid", "manifest": str(path) if path else None, "requests": len(manifest.requests), "sources": sources}
        elif args.command == "execute":
            manifest, path = _manifest_from_args(args, config)
            if args.wait_ready and not (args.publish or args.publish_dry_run):
                raise ExactCutError("--wait-ready 必须同时使用 --publish")
            payload = execute_batch(
                args.project, manifest, config, request_ids=args.request_id, force=args.force,
                publish=args.publish or args.publish_dry_run, wait_ready=args.wait_ready,
                publish_dry_run=args.publish_dry_run,
            )
            payload["manifest"] = str(path) if path else None
        elif args.command == "ensure-feishu":
            payload = FeishuClient().ensure_schema(dry_run=args.dry_run)
        else:
            payload = sync_record_once(args.record_id)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        if args.command == "execute":
            statuses = [str(item.get("status") or "") for item in (payload.get("requests") or {}).values()]
            if any(status in {"failed", "publish_failed"} for status in statuses):
                return 2
        return 0
    except (ExactCutError, FileNotFoundError, ValueError, RuntimeError) as exc:
        from .stage_results import ResourceDeferred
        if isinstance(exc, ResourceDeferred): raise
        print(json.dumps({"status": "error", "error_type": type(exc).__name__, "error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
