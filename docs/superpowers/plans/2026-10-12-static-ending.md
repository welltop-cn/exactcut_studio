# Static ending implementation plan

**Goal:** Append an optional silent still to each cut, from its last frame or an uploaded image, with optional guide text and a configurable duration (default 3 seconds).

**Architecture:** Keep timeline/base-cut caching unchanged. Add optional `ending` to the version-1 request contract. Draft fields are `mode` (`none`, `last_frame`, `image`), `image` (local basename), `guide` (up to 200 characters), and `duration_ms` (1000–15000). Missing mode means disabled, including legacy compositions. Snapshot image bytes by SHA-256 into the drama's `assets/endings` at confirmation. Append the still after Hook rendering; return combined expected duration and a separate ending audit record.

**Tech Stack:** Existing Python/Pydantic, Pillow, FFmpeg and vanilla JavaScript. No external services or new dependencies.

- [x] Add failing tests in `source/tests/test_static_ending.py`: request validation/defaults, draft compilation and duration, image snapshot integrity, actual last-frame/image rendering, silent audio and base-cache reuse.
- [x] Implement `source/exactcut/ending.py`, integrate `models.py`, `intake.py`, `workflow.py` and `material_versions.py`. Reuse existing upload routes and stored image references; require decodable images before confirmation.
- [x] Integrate `renderer.py` and `orchestrator.py`: resolve checked image inputs, generate normalized still clip, concatenate after the Hook output, include duration and audit metadata. Never change the base timeline to add the ending.
- [x] Add editor controls to both ordinary and structured rows in `source/exactcut/web/app.js`, including immediate timing updates and preview of uploaded image. Persist ordinary ending fields without creating a composition.
- [x] Verify browser interactions, real FFmpeg output (last frame matches, image is contained, text visible, audio silent), version/clone behavior and regression suite. Document behavior in `source/README.md`.

Run focused tests from `source` with `/Users/matao/miniconda3/envs/py3.12/bin/python3.12 -m pytest tests/test_static_ending.py`; first expect missing-feature failures, then all passing. Run the existing suite after integration. Work remains in the isolated development repository; deployment is outside this change.
