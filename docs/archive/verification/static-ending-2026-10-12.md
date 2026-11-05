# Static ending development verification

> 历史归档（2026-11-04）。正文保留当时的方案或执行状态，不代表当前版本；日常操作见[文档入口](../../README.md)。

Scope: development repository only. User explicitly requested no release/deployment until further notice. No production service, configuration or media was changed.

## Behavior

- Both ordinary and structured material editors expose an optional static ending.
- Modes: disabled (default/legacy), last cut frame, uploaded image. Default duration 3 seconds; accepted range 1–15 seconds. Optional multiline guide text up to 200 characters, fitted white text with black outline in the lower safe area.
- Images remain uncropped, with black padding when needed. Supported PNG/JPEG/WebP, at most 4 MB / 32 million pixels. Decode and integrity checks run before confirmation.
- Ending audio is silent. Timing projection, render plan and output QC include appended duration. Source episode ranges stay unchanged.
- Content-addressed confirmed image snapshots reside under the drama project `assets/endings/`; files and their directory entries are synchronized before manifest publication. Both legacy and managed clone paths retain referenced inputs.
- Ending-only changes reuse the base cut. Ending SAR matches the actual hooked stream so compatible episode/ending joins use the existing packet-copy path.
- Image uploads disable editing/save/confirmation while pending; per-row tokens and navigation guards reject stale completions. Preview cache is scoped by project and image name.

## Evidence

- `source/tests/test_static_ending.py`: 10 feature tests cover contract/defaults, ordinary/structured draft compilation, duration, decoded images, immutable snapshots and directory synchronization, ordinary/managed confirmation and cloning, browser controls/upload preview/order/project switching, frame equality, audio silence, base reuse and one-frame cut boundaries.
- Focused regression after review fixes: **110 passed** (`test_static_ending.py`, `test_managed_editor_review.py`, `test_material_versions.py`, `test_phase2_hook_workflow.py`).
- Final complete isolated suite after review fixes: **1090 passed in 179.56 seconds**, exit code 0. Log: `/tmp/exactcut-static-ending-verified-tests.log`.
- `node --check source/exactcut/web/app.js` and `git diff --check` pass.
- Independent read-only review identified upload ordering, directory durability and SAR fallback issues. These were reproduced, fixed and re-reviewed; no remaining important findings in that pass.

Run the complete suite from `source/` with a new empty temporary data directory:

```sh
static_ending_test_root=$(mktemp -d /tmp/exactcut-ending-tests.XXXXXX)
EXACTCUT_DATA_ROOT="$static_ending_test_root" ~/miniconda3/envs/py3.12/bin/python3.12 -m pytest
```

The explicit data root avoids tests that temporarily use the production environment colliding with the development marker in the working source directory. Environment guards remain unchanged.

## Local dev UI-to-video acceptance

- Service: localhost:8793, health environment=development, worker alive. Independent project: `40f7b5c4d50f44478e469e67aa78c699` (结尾设置验收 · 定格与图片).
- Used existing dev EP1 of 101606, 00:00–00:08. Entered all clip/ending settings through UI events, uploaded a PNG extracted from that same dev episode, saved, reloaded, and confirmed both endings and decoded image preview persisted without validation errors.
- Submitted actual preparation/render jobs through the UI confirmation dialog. Result: 2 assets qc_passed, 0 failures.
- R40f7b5c4: last_frame, 3000 ms, two-line Chinese/English guide; actual 1080×1920 MP4 duration 11.000 s.
- R03574622: uploaded PNG, 4000 ms, two-line Chinese/English guide; actual 1080×1920 MP4 duration 12.000 s.
- Decoded final frames manually inspected; both contain the selected background and two-line white text with black stroke. 9 s versus 10 s frame per-channel mean differences below 0.003/255 (encoding variation). PCM sampled from 8.3 s for 2 s was all zero bytes in both videos, verifying silent endings.
- Authenticated preview requests returned HTTP 206, video/mp4, with requested 1024-byte ranges. Background Chrome tabs deferred native video loading (readyState 0); this is not recorded as completed browser playback. Actual MP4s were decoded using FFmpeg and retained in local dev for user playback.
- No production changes or publishing; no code changes required during this acceptance run.
