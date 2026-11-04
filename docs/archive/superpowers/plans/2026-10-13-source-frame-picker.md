# Source frame picker implementation plan

> 历史归档（2026-11-04）。正文保留当时的方案或执行状态，不代表当前版本；日常操作见[文档入口](../../../README.md)。

> **For agentic workers:** Use subagent-driven-development for implementation and independent specification/code reviews.

**Goal:** Fill episode/time first, reuse source prefetch, then inspect video, drag, step actual source frames, mark bounds, and explicitly apply to the draft; verify in dev only.

**Architecture:** Keep durable millisecond clips and source-prefetch contract. A batch-owned source picker cache indexes actual presentation timestamps and serves authenticated original video and verified frame images. A separate browser module owns the transient selection and refuses stale application after navigation/edits. No production operations or automatic publication.

**Tech Stack:** Existing Python HTTP server, FFmpeg/ffprobe, vanilla JS, pytest and Playwright.

## Task 1: Source frame API
- [ ] Add real-FFmpeg failing tests for actual frame timestamp indexing, first/last frame, noninteger frame rate, stale source identity, invalid indexes and authorization.
- [ ] Implement `source/exactcut/source_picker.py`: bound concurrent media inspection, derive frame timestamps from ffprobe, fingerprint source, cache frame metadata/images, resolve only valid draft-referenced episodes.
- [ ] Add POST `/api/batches/{id}/source-picker` with `{draft,episode}`; return documented metadata, timestamp boundaries, cache key. GET `/api/batches/{id}/source-picker/{key}/video` and `/frame/{index}` require batch access and valid current source identity. Avoid holding mutation lock during media work. Missing sources produce safe actionable errors.
- [ ] Validate rendered frame images against sequential-decoding reference, including B-frames. Preserve existing rendering/storage contracts.

## Task 2: Editor interaction
- [ ] Write failing browser tests for mark/apply, cancel, delayed response fences, exact-image readiness and frame-end exclusion.
- [ ] Add `source/exactcut/web/source-picker.js`; integrate script and button rendering with existing clip/composition editor. Show preparation status, open/retry action; reuse 600ms prefetch debounce and shared cache.
- [ ] Dialog shows original video, whole-source range, zoomed local range, current exact frame, previous/next frame, mark start/end, selection playback, apply/cancel. End marking includes displayed frame by selecting next boundary. Existing manual milliseconds remain unchanged until apply.
- [ ] Bind requests to project/navigation/drama/clip snapshot, disable markers until exact image loaded, clamp first/last positions, reject invalid/empty intervals and source changes.

## Task 3: Dev acceptance
- [ ] Independent specification then quality review; fix findings and run focused plus full regression.
- [ ] Inspect active dev processes/tasks before restarting shared 8793. Preserve independent dev environment and disabled external writes/downloads. Use copied local dev video; never production credentials or production tasks.
- [ ] Verify actual browser dragging, stepping, marks, cancellation, save/reopen, HTTP Range video playback and missing-source state. Record screenshots, frame evidence, test commands and actual running revision/path; deliver dev URL.

## Approved interaction revision (2026-10-13)

- Preserve two static endpoint previews. No active-card switching is required.
- Two thin movable vertical lines, labeled start/end grips, a visible time ruler and shaded retained interval. Drag either line directly to update its preview; keyboard arrows step the focused endpoint. The other endpoint does not change. This supersedes the briefly proposed single-playhead/card activation model.
- Cancel/Apply remain visible. Existing projects retain their original configuration and millisecond interpretation without migration.
- New manual projects explicitly request `source_frames_v1`; draft stores `editor_time_format` and clips store `time_format` plus `*_f`. Frame ordinal is zero based within each actual presentation second; inclusive end is converted to the next source boundary for the existing millisecond renderer.
- No assumed 30fps. Missing indexes allow draft saving and source preparation, but block rendering until actual source bounds can be validated. New demo will be a separate local project using the same source assets/config, not modifying the copied legacy project.
- Backend/UI regression must cover new/legacy formats, preservation of inactive endpoints, stale callbacks, empty source intervals, and source identity changes. Dev-only delivery; no commits/merge/release.
