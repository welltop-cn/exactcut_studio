# Hook Typography Implementation Plan

**Goal:** Allow users to select bundled fonts, text/accent colors and an explicit font size for Hook titles, with matching preview and final rendering.

**Architecture:** Extend the existing HookOptions contract with optional overrides; defaults keep preset behavior. Apply overrides in shared presentation/rendering, include them in existing preview identity, validate layout before starting. No durable schema version change or external service.

**Tech Stack:** Python/Pydantic/Pillow, existing JavaScript editor, pytest.

## Tasks
- [x] Add failing tests in source/tests/test_hook_typography.py for contract validation, rendered font/size/color pixels, safety overflow, draft persistence and preview invalidation.
- [x] Extend source/exactcut/hook_options.py with font (auto/sans/sans_italic/serif/cjk), text_color/accent_color (#RRGGBB or null), font_size (32–96 px at 1080 width or null).
- [x] Apply overrides in hook_visual.py and hook_presentation.py. Explicit size scales with output width and fails rather than silently shrinking beyond safe layout. Existing font manifest remains authoritative.
- [x] Make intake validation use selected typography; preserve overrides during composition compilation.
- [x] Add visible typography controls next to Hook input in web/app.js, with preset reset, color pickers, optional size and accessible labels; retain preview invalidation and draft locking.
- [x] Run focused pytest suites and JS syntax check; exercise local dev save/preview and inspect generated images. Record evidence. 用户要求先更新本地服务验收；前端存在其他会话并发修改，本轮保留未提交状态。

Validation command: `cd source && .venv/bin/python -m pytest tests/test_hook_typography.py tests/test_phase1_hook.py tests/test_phase2_hook_workflow.py tests/test_phase2_preview_api.py tests/test_hook_visual.py tests/test_workflow.py -q`.
