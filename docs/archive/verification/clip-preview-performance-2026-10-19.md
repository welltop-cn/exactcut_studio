# Clip preview performance — 2026-10-19

> 历史归档（2026-11-04）。正文保留当时的方案或执行状态，不代表当前版本；日常操作见[文档入口](../../README.md)。

- Preview now trims and downsizes to a maximum 640-pixel long edge before composition; frame rate, clip frame count and audio presence checked. Full output remains original resolution. Preview identity versioned to avoid returning previous high-resolution cache.
- Raw inference cache excludes appearance but includes source hash, reset, geometry, reference frame and tracker configuration. Batch inference skips unused per-target video encoding. Existing render masks are reused only after checking stored request configuration, source, metadata, geometry and reset; incomplete masks rejected.
- User's completed 300-frame / 10-second clip was composed with existing two masks into an isolated temporary file: 10.09 seconds, 360×640, 30 fps, 300 frames, audio present. Previous job timestamps indicate approximately 73 seconds after tracking. This is not a controlled cold-inference comparison or a guaranteed latency.
- Read-only compatibility check verified both existing targets are reused with changed appearance and a tracker stub that fails if inference is invoked. User drafts, source and previous result unchanged.
- Render/tracking/effects suite: 28 passed; additional legacy-cache regression: passed. UI suite passed during combined run (three backend frame-rate regressions found and corrected by explicit CFR output; four preview/inference tests then passed). Added cache-effect independence, geometry/config/reset invalidation and actual FFmpeg downscale tests.
