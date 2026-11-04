# Frame project clone acceptance — 2026-10-13

> 历史归档（2026-11-04）。正文保留当时的方案或执行状态，不代表当前版本；日常操作见[文档入口](../../README.md)。

## Problem and fix

Cloning copied timeline inputs but omitted the per-project `source-picker/*/index.json` required to resolve source frame ordinals into the existing millisecond manifest. Both rows of test clone `975a9095c6014639b8349962e0538e8a` failed validation.

`workflow._clone_batch` now calls `source_picker.copy_frame_indexes`. Only referenced frame-mode episodes with a matching current source fingerprint and internally consistent index are copied atomically into independent files. Symlink indexes and stale sources are not copied. No frame images, original video, completed output or execution state is copied. Legacy millisecond input remains unchanged. No probing is performed under the clone naming lock.

## Server acceptance

Only `exactcut-test` code was updated. Test container had no active media processes before restart. Production container retained StartedAt `2026-10-12T13:53:04.32042892Z` and health status ok.

- Existing clone `975a9095c6014639b8349962e0538e8a`: restored five valid derived indexes from its source; draft bytes unchanged; errors empty; remains draft.
- New clone through actual `workflow.clone_batch`: `b64e4bd4b8a8419eb4f0337d9b095003`; five own indexes; errors empty; timelines identical; remains draft.
- Source `95f99b49174e4cbb80dbbe381abde05c` remains paused.
- Authenticated public HTTP `/test/api/batches/<id>` returned 200 and empty errors for both clones.
- No video jobs submitted, FBBM/Feishu writes, or production restart during this acceptance. This acceptance proves clone validation, not a new complete rendering run.

## Regression and review

20 focused clone/timecode/legacy tests passed. Coverage includes managed and legacy lifecycle clones, clone-of-clone, independence after source index removal, replaced original video and symlink rejection.

Initial full regression caught stale browser tests attempting to click the intentionally hidden picker. Component tests now explicitly expose the retained component in their fixture; a separate test uses actual product CSS to require the entry and hint stay hidden. Time hint assertions reflect manual frame entry.

Final full-suite result and Grok review are recorded below after completion.

## Grok review finding and follow-up

Actual Grok CLI session `8075840d-e4c3-4ff5-a4a6-8e316b200b9e` approved clone index handling, but found managed `_receipt` still compared against hardcoded production preview PREFIX. Fixed it to use centralized `object_prefix()` just like the uploader. Added production/test acceptance and opposite-namespace rejection tests; test namespace failed before the fix. Related suite: 126 passed.

Repeated server acceptance after updating only test `material_workers.py`: uploaded existing one-second synthetic acceptance video through real OSS client into `exactcut-previews/test/`; managed `_receipt` accepted the real receipt and rejected the production-prefix variant. Source remained paused, clone errors remained empty. Initial direct `docker exec` probe omitted environment-file loading and failed before network upload; rerun used existing `load_env_file('/production.env')`, matching server startup, and passed. No credentials recorded.

Intermediate full suite before this two-line receipt fix: 1272 passed, six existing fork warnings, 234.87s. Final full suite pending below.

This branch is accepted for isolated testing/commit only. Downgrade protection for frame-schema drafts remains a prerequisite before formal release; do not roll back live frame projects to a pre-frame implementation.

Grok follow-up reviewed `_receipt`, production/test rejection coverage and hidden-entry browser fixture; explicitly confirmed the previous blocker resolved and approved commit for the isolated test instance. Parent agrees. Review does not authorize production release; final regression remains required below.

## Final result

Final code complete local regression: **1274 passed**, six existing multiprocessing fork deprecation warnings, **233.10s**, exit 0. `git diff --check` passed. After the receipt fix, repeated authenticated public test API checks returned HTTP200/empty errors for both clones; worker health was ok. Review blockers resolved, parent and Grok agree to commit. Original paused project was not resumed and no real production deployment was performed.
