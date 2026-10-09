from __future__ import annotations

import base64
import io
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx
from PIL import Image, ImageDraw

from .errors import SafetyError
from .media import bounded_ffmpeg, duration_ms, load_shots, resolve_episode_video
from . import ai_rate
from .stage_results import ResourceDeferred
from .util import file_sha256, json_sha256, now_iso, write_json_atomic


REDLINE_PROMPT = """
You are a fail-closed visual safety inspector for paid-social drama video.
Inspect only what is visibly present. Reject the entire physical shot when any frame shows:
1. recognizable external genitals or anus;
2. genitals strategically covered only by a hand, towel, tiny cloth/object, blur, crop edge or similar concealment;
3. large-area nudity extending from chest/abdomen into pelvis, buttocks or thighs.
Normal opaque underwear or pants are not strategic covering. Kissing, licking, intimate positioning,
simulated intimacy or undressing actions alone are not rejection reasons unless actual prohibited exposure is visible.
Return JSON only:
{"decision":"pass|reject","categories":[],"evidence_zh":"","confidence":0.0}
""".strip()


FINAL_VIDEO_PROMPT = """
Review the entire uploaded paid-social video. Apply these permanent visual red lines with no duration,
area or confidence exemption: recognizable external genitals/anus; genital-area strategic covering by a
hand, towel, tiny cloth/object, blur or crop edge; large-area nudity extending from chest/abdomen into
pelvis, buttocks or thighs. Normal opaque underwear/pants are allowed. Sexual implication without actual
prohibited visible exposure is allowed. Return JSON only:
{"summary_zh":"","risk_events":[{"category":"","start_ms":0,"end_ms":0,"evidence_zh":""}]}
""".strip()


GPT_FRAME_PROMPT = """
You are the second independent fail-closed safety reviewer. The contact sheets cover the opening,
uniform samples, risk-adjacent moments, and both sides of every edit boundary. Reject if any image visibly
contains recognizable external genitals/anus, strategic genital covering by hand/towel/tiny object/blur/crop,
or large-area nudity extending into pelvis/buttocks/thighs. Do not reject ordinary opaque underwear/pants,
kissing, intimate positions, or implication without actual prohibited exposure. Return JSON only:
{"decision":"pass|reject","findings":[{"frame_label":"","category":"","evidence_zh":""}],"summary_zh":""}
""".strip()


def _json_object(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0 or end <= start:
            raise SafetyError("模型没有返回JSON对象")
        value = json.loads(cleaned[start : end + 1])
    if not isinstance(value, dict):
        raise SafetyError("模型返回值不是JSON对象")
    return value


def _gemini_client() -> Any:
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise SafetyError("缺少 GEMINI_API_KEY")
    from google import genai

    from google.genai import types
    return genai.Client(api_key=api_key, http_options=types.HttpOptions(
        retry_options=types.HttpRetryOptions(attempts=1)))


def _extract_frame(video: Path, timestamp_ms: int) -> Image.Image:
    command = [
        "ffmpeg", "-v", "error", "-ss", f"{timestamp_ms / 1000:.3f}", "-i", str(video),
        "-frames:v", "1", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1",
    ]
    result = subprocess.run(bounded_ffmpeg(command), capture_output=True)
    if result.returncode or not result.stdout:
        raise SafetyError(f"无法提取安全检查帧：{video.name}@{timestamp_ms}ms")
    return Image.open(io.BytesIO(result.stdout)).convert("RGB")


def _contact_sheet(frames: list[tuple[str, Image.Image]], columns: int = 3) -> bytes:
    if not frames:
        raise SafetyError("没有可生成联系表的画面")
    thumb_w, thumb_h, label_h = 480, 270, 34
    rows = (len(frames) + columns - 1) // columns
    sheet = Image.new("RGB", (thumb_w * columns, (thumb_h + label_h) * rows), "white")
    draw = ImageDraw.Draw(sheet)
    for index, (label, frame) in enumerate(frames):
        x = (index % columns) * thumb_w
        y = (index // columns) * (thumb_h + label_h)
        thumb = frame.copy()
        thumb.thumbnail((thumb_w, thumb_h))
        canvas = Image.new("RGB", (thumb_w, thumb_h), "black")
        canvas.paste(thumb, ((thumb_w - thumb.width) // 2, (thumb_h - thumb.height) // 2))
        sheet.paste(canvas, (x, y))
        draw.text((x + 8, y + thumb_h + 8), label[:70], fill="black")
    buffer = io.BytesIO()
    sheet.save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


def _gemini_images_json(client: Any, model: str, prompt: str, images: list[bytes]) -> dict[str, Any]:
    from google.genai import types

    contents = [types.Part.from_text(text=prompt)]
    contents.extend(types.Part.from_bytes(data=image, mime_type="image/jpeg") for image in images)
    ai_rate.acquire()
    response = client.models.generate_content(
        model=model,
        contents=contents,
        config=types.GenerateContentConfig(response_mime_type="application/json"),
    )
    return _json_object(response.text or "")


def _expand_episode_item(project_dir: Path, item: dict[str, Any]) -> list[dict[str, Any]]:
    episode = str(item["episode"]).upper()
    start_ms, end_ms = int(item["start_ms"]), int(item["end_ms"])
    shots = load_shots(project_dir, episode)
    overlaps = [shot for shot in shots if shot["start_ms"] < end_ms and shot["end_ms"] > start_ms]
    if not overlaps:
        raise SafetyError(f"{episode} {start_ms}-{end_ms} 未映射到物理镜头")
    units = []
    cursor = start_ms
    for shot in overlaps:
        unit_start = max(start_ms, int(shot["start_ms"]))
        unit_end = min(end_ms, int(shot["end_ms"]))
        if unit_start > cursor + 1:
            raise SafetyError(f"{episode} 物理镜头存在未覆盖间隙：{cursor}-{unit_start}")
        if unit_end > unit_start:
            units.append({
                "type": "episode",
                "episode": episode,
                "start_ms": unit_start,
                "end_ms": unit_end,
                "duration_ms": unit_end - unit_start,
                "shot_id": shot["shot_id"],
                "context": {
                    "ocr_text": shot.get("ocr_text", ""),
                    "asr_text": shot.get("asr_text", ""),
                    "merged_text": shot.get("merged_text", ""),
                    "action_tags": shot.get("action_tags", []),
                    "emotion_tags": shot.get("emotion_tags", []),
                },
            })
            cursor = unit_end
    if cursor < end_ms - 1:
        raise SafetyError(f"{episode} 物理镜头未覆盖尾部：{cursor}-{end_ms}")
    return units


def editorial_preflight(project_dir: Path, request: Any, config: dict[str, Any], report_path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    state = config["safety"]
    client = _gemini_client()
    expanded: list[dict[str, Any]] = []
    for raw in request.timeline:
        item = raw.model_dump(mode="json") if hasattr(raw, "model_dump") else dict(raw)
        if item["type"] == "episode":
            expanded.extend(_expand_episode_item(project_dir, item))
        else:
            expanded.append(item)
    policy = json_sha256({"prompt": REDLINE_PROMPT, "models": state})
    source_fingerprint = []
    for item in expanded:
        if item["type"] == "episode":
            source = resolve_episode_video(project_dir, item["episode"])
            source_fingerprint.append([str(source), file_sha256(source), item["start_ms"], item["end_ms"], item["shot_id"]])
    fingerprint = json_sha256({"policy": policy, "source": source_fingerprint})
    if report_path.exists():
        cached = json.loads(report_path.read_text(encoding="utf-8"))
        if cached.get("fingerprint") == fingerprint and cached.get("status") in {"passed", "rejected"}:
            if cached.get("status") != "passed":
                raise SafetyError(cached.get("error") or "缓存的物理镜头安全预检未通过")
            return cached["safe_timeline"], cached

    decisions = []
    safe_timeline = []
    removed = []
    samples = max(3, int(state.get("shot_samples", 3)))
    for index, item in enumerate(expanded):
        if item["type"] != "episode":
            safe_timeline.append(item)
            continue
        source = resolve_episode_video(project_dir, item["episode"])
        start, end = int(item["start_ms"]), int(item["end_ms"])
        timestamps = [round(start + (end - start) * ratio) for ratio in (0.05, 0.5, 0.95)]
        if samples > 3:
            timestamps = [round(start + (end - start) * ((i + 0.5) / samples)) for i in range(samples)]
        frames = [(f"{item['shot_id']}@{stamp}ms", _extract_frame(source, stamp)) for stamp in timestamps]
        sheet = _contact_sheet(frames)
        last_error: Exception | None = None
        decision = None
        identity = {'fingerprint': fingerprint, 'item': item, 'samples': samples}
        shot_path = report_path.parent / 'safety-shot-cache' / (json_sha256(identity) + '.json')
        if shot_path.exists():
            try:
                cached = json.loads(shot_path.read_text(encoding='utf-8'))
                if (cached['identity'] == identity and cached['response_sha256'] == json_sha256(cached['response'])
                        and cached['response'].get('decision') in {'pass', 'reject'}):
                    decision = cached['response']
            except (ValueError, KeyError, TypeError, OSError):
                pass
        for attempt in range(0 if decision is not None else int(state.get("request_attempts", 3))):
            try:
                decision = _gemini_images_json(client, state["editorial_model"], REDLINE_PROMPT, [sheet])
                if decision.get('decision') not in {'pass', 'reject'}:
                    raise SafetyError('物理镜头安全检查返回无效决定')
                write_json_atomic(shot_path, {'identity': identity, 'response': decision,
                                             'response_sha256': json_sha256(decision)})
                break
            except ResourceDeferred:
                raise
            except Exception as exc:
                last_error = exc
                time.sleep(attempt + 1)
        if decision is None:
            raise SafetyError(f"物理镜头安全检查未完成：{last_error}")
        rejected = str(decision.get("decision") or "").lower() != "pass"
        row = {"index": index, "shot_id": item["shot_id"], "source_window_ms": [start, end], **decision}
        decisions.append(row)
        if rejected:
            removed.append(row)
        else:
            safe_timeline.append(item)

    if not any(item["type"] == "episode" for item in safe_timeline):
        raise SafetyError("安全预检删除了全部正片镜头")
    story_review: dict[str, Any] = {
        "story_readable": True,
        "who_and_conflict_clear": True,
        "consequence_preserved": True,
        "rationale_zh": "没有删除镜头。",
    }
    if removed:
        context = [
            {"episode": item.get("episode"), "window": [item.get("start_ms"), item.get("end_ms")], "context": item.get("context", {})}
            for item in safe_timeline if item["type"] == "episode"
        ]
        prompt = (
            "你是短剧叙事连贯性审核员。部分物理镜头因永久安全红线被删除。仅根据保留镜头顺序与上下文判断："
            "冷观众能否在3秒内理解谁与什么冲突，以及结尾是否仍保留角色+动作+后果的具体悬念。"
            "不得为了保留不安全镜头而判通过。返回JSON："
            '{"story_readable":true,"who_and_conflict_clear":true,"consequence_preserved":true,"rationale_zh":""}\n'
            f"标题：{request.title_cn}\nHook：{request.hook_text}\n保留时间线：{json.dumps(context, ensure_ascii=False)}"
        )
        story_review = _gemini_images_json(client, state["story_model"], prompt, [])
        if not all(bool(story_review.get(key)) for key in ("story_readable", "who_and_conflict_clear", "consequence_preserved")):
            raise SafetyError(f"删镜头后叙事不可读：{story_review.get('rationale_zh', '')}")

    report = {
        "checked_at": now_iso(), "status": "passed", "allowed": True, "fingerprint": fingerprint,
        "policy_fingerprint": policy, "decisions": decisions, "removed": removed,
        "story_review": story_review, "safe_timeline": safe_timeline,
    }
    write_json_atomic(report_path, report)
    return safe_timeline, report


def final_video_gemini(video: Path, config: dict[str, Any], report_path: Path) -> dict[str, Any]:
    state = config["safety"]
    digest = file_sha256(video)
    policy = json_sha256({"prompt": FINAL_VIDEO_PROMPT, "model": state["final_video_model"]})
    identity = {'video_sha256': digest, 'policy_fingerprint': policy}
    checkpoint = report_path.parent / 'gemini-uploads' / (json_sha256(identity) + '.json')
    if report_path.exists():
        cached = json.loads(report_path.read_text(encoding="utf-8"))
        if cached.get("video_sha256") == digest and cached.get("policy_fingerprint") == policy and cached.get("status") in {"passed", "rejected"}:
            # A crash after persisting the report can leave only remote cleanup outstanding.
            if checkpoint.exists():
                try:
                    upload = json.loads(checkpoint.read_text(encoding='utf-8'))
                    if upload['identity'] == identity:
                        _gemini_client().files.delete(name=upload['name'])
                        checkpoint.unlink()
                except Exception:
                    pass
            return cached
    client = _gemini_client()
    from google.genai import types

    uploaded_name = None
    try:
        if checkpoint.exists():
            upload = json.loads(checkpoint.read_text(encoding='utf-8'))
            if upload['identity'] != identity or not isinstance(upload['name'], str) or not upload['name']:
                raise SafetyError('Gemini 上传检查点无效')
        else:
            with video.open("rb") as handle:
                uploaded = client.files.upload(file=handle, config=types.UploadFileConfig(mime_type="video/mp4", display_name=video.name))
            upload = {'version': 1, 'identity': identity, 'name': uploaded.name, 'uploaded_at': time.time()}
            write_json_atomic(checkpoint, upload)
        uploaded_name = upload['name']
        info = client.files.get(name=uploaded_name)
        file_state = info.state.name if hasattr(info.state, "name") else str(info.state)
        if file_state != "ACTIVE":
            if file_state == "FAILED" or time.time() >= upload['uploaded_at'] + 1200:
                raise SafetyError(f"Gemini无法处理成片，state={file_state}")
            raise ResourceDeferred(time.time() + 3, 'Gemini 正在处理成片，稍后继续安全检查')
        # File lifecycle queries are not model requests. Charge immediately before inference.
        ai_rate.acquire()
        response = client.models.generate_content(
            model=state["final_video_model"],
            contents=[types.Part.from_uri(file_uri=info.uri, mime_type="video/mp4"), types.Part.from_text(text=FINAL_VIDEO_PROMPT)],
            config=types.GenerateContentConfig(response_mime_type="application/json"),
        )
        analysis = _json_object(response.text or "")
        violations = analysis.get("risk_events") or []
        report = {
            "checked_at": now_iso(), "video_sha256": digest, "policy_fingerprint": policy,
            "status": "rejected" if violations else "passed", "allowed": not violations,
            "model": state["final_video_model"], **analysis,
        }
    except ResourceDeferred:
        # Keep the remote handle and local checkpoint across processing/quota deferrals.
        raise
    except Exception as exc:
        report = {"checked_at": now_iso(), "video_sha256": digest, "policy_fingerprint": policy, "status": "unverified", "allowed": False, "error": str(exc)}
    write_json_atomic(report_path, report)
    if uploaded_name:
        try:
            client.files.delete(name=uploaded_name)
            checkpoint.unlink(missing_ok=True)
        except Exception:
            pass
    return report


def _openai_output_text(payload: dict[str, Any]) -> str:
    if payload.get("output_text"):
        return str(payload["output_text"])
    parts = []
    for item in payload.get("output") or []:
        for content in item.get("content") or []:
            if content.get("type") in {"output_text", "text"}:
                parts.append(str(content.get("text") or ""))
    return "\n".join(parts)


def final_video_gpt_frames(video: Path, boundaries_ms: list[int], config: dict[str, Any], report_path: Path) -> dict[str, Any]:
    state = config["safety"]
    digest = file_sha256(video)
    policy = json_sha256({"prompt": GPT_FRAME_PROMPT, "model": state["gpt_frame_model"], "boundaries": boundaries_ms})
    if report_path.exists():
        cached = json.loads(report_path.read_text(encoding="utf-8"))
        if cached.get("video_sha256") == digest and cached.get("policy_fingerprint") == policy and cached.get("status") in {"passed", "rejected"}:
            return cached
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        report = {"status": "unverified", "allowed": False, "error": "缺少 OPENAI_API_KEY", "video_sha256": digest, "policy_fingerprint": policy}
        write_json_atomic(report_path, report)
        return report
    total = duration_ms(video)
    interval = max(250, round(float(state.get("uniform_frame_interval_s", 1.0)) * 1000))
    stamps = {0, min(total - 1, 250), min(total - 1, 500), min(total - 1, 1000)}
    stamps.update(range(0, max(1, total), interval))
    margin = round(float(state.get("boundary_margin_s", 0.75)) * 1000)
    for boundary in boundaries_ms:
        stamps.update({max(0, boundary - margin), max(0, boundary - 1), min(total - 1, boundary + 1), min(total - 1, boundary + margin)})
    frames = [(f"t={stamp}ms", _extract_frame(video, stamp)) for stamp in sorted(stamp for stamp in stamps if 0 <= stamp < total)]
    sheets = [_contact_sheet(frames[index : index + 12], columns=3) for index in range(0, len(frames), 12)]
    content: list[dict[str, Any]] = [{"type": "input_text", "text": GPT_FRAME_PROMPT + "\nFrame labels are printed beneath each image."}]
    for sheet in sheets:
        encoded = base64.b64encode(sheet).decode("ascii")
        content.append({"type": "input_image", "image_url": f"data:image/jpeg;base64,{encoded}", "detail": "high"})
    request = {
        "model": state["gpt_frame_model"],
        "reasoning": {"effort": state.get("gpt_reasoning_effort", "high")},
        "input": [{"role": "user", "content": content}],
        "text": {"format": {"type": "json_object"}},
        "max_output_tokens": 4096,
    }
    endpoint = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/") + "/responses"
    try:
        ai_rate.acquire()
        with httpx.Client(timeout=180) as client:
            response = client.post(endpoint, headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, json=request)
            response.raise_for_status()
            analysis = _json_object(_openai_output_text(response.json()))
        rejected = str(analysis.get("decision") or "").lower() != "pass" or bool(analysis.get("findings"))
        report = {
            "checked_at": now_iso(), "video_sha256": digest, "policy_fingerprint": policy,
            "status": "rejected" if rejected else "passed", "allowed": not rejected,
            "model": state["gpt_frame_model"], "sample_count": len(frames), "boundary_count": len(boundaries_ms), **analysis,
        }
    except ResourceDeferred:
        raise
    except Exception as exc:
        report = {"checked_at": now_iso(), "video_sha256": digest, "policy_fingerprint": policy, "status": "unverified", "allowed": False, "error": str(exc)}
    write_json_atomic(report_path, report)
    return report
