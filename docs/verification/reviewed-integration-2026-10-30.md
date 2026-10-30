# 2026-10-29–30 changes: integration review and local acceptance

Scope pinned at main `db4c684` on 2026-10-30, Asia/Shanghai. The previous calendar-window baseline is `875c237`; production was independently read as `us-east-20261028-originals-7960491` / commit `7960491`. The coordinator performed this review and the additional live dev checks; no D2 delegation was used.

## Branch/worktree inventory

All worktree records, local branch refs and six historical stashes were inspected. Dirty tracked patches and nonignored untracked files were backed up before integration at `/Users/matao/.config/superpowers/release-prep/exactcut-20261030-1802/`.

| Work | Disposition |
|---|---|
| Material validation locations, focus after unlock, clone validation (`aebc64a`, `6850574`, `db4c684`) | Already on main; retained and included in integrated tests. |
| `fix/editor-busy-retry` (`b400bfc`) | Merged into isolated candidate; both previous blockers and the narrow retry contract reviewed. |
| `feat/preview-subtitles` | Previously dirty implementation committed as `6ff3493`; merged into candidate, preserving UI alignment, two workers and preview-only output. |
| Approved UI/spec/local notes and two static design prototypes | Committed on main as `7c96f94`; merged into candidate. Prototype HTML is reference material, not a new production UI flow. |
| `experiment/preview-subtitle-demo` | Code, tests and survey preserved in separate experimental commit `0cc399d`; 7 focused tests passed. Experimental server module is excluded from the production candidate/wheel. |
| `feat/source-frame-picker`, `feat/visual-frame-picker` | Historical visual-picker work; explicit deferral in `3f8b094` and the October 13 integration review. Not re-enabled. Main already retains the approved frame-text/index core. |
| `feat/mosaic-sam2` dirty scratch, detached `restore-stash-1465454`, archived cutting work and six stashes | October 12–16 history, not October 29–30 work. Preserved without merging obsolete experiments. |
| Other admin/Feishu/OSS/project-init/mosaic/render/review/release branches and detached OSS/originals worktrees | Tips are already ancestors of main; no duplicate merge required. |
| Missing/prunable diagnostics acceptance worktree | Recorded, not pruned during this task. |
| Older untracked manual/screenshots, SAM research and production-smoke note in the main working directory | Outside the date window; preserved untouched and excluded from the release artifact. |

Running 8793, 8800, 8801, 8810, 8820 and 8821 services/data were not restarted. A separate 8822 review instance was created with independent accounts, registry and copied video data.

## Standards review

Applicable standards: `source/AGENTS.md`, environment/data isolation in `exactcut/runtime.py`, existing centralized config, application-owned HTTP contracts and the Linux release-manager schema/maintenance rules.

Two concrete issues were found and fixed:

1. Subtitle recognition did not participate in release task-drain checks. Added running subtitle reports and both durable active slots to the manager's gate; invalid queue state fails closed. POST enqueue and dispatcher claim now recheck maintenance inside the existing short mutation barrier. Pending tasks pause and survive the switch; paid calls are not automatically repeated. Six failing reproductions were observed before the fix; all seven release-specific regressions pass afterward. Commit `f779a37`.
2. The built wheel omitted the already-served `diagnostics.js`. A real wheel audit and a new served-script/package-data regression both reproduced the omission. Resource declaration fixed; rebuilt wheel includes every served module. Commit `f6de4e1`.

Merge conflicts were limited to the HTML script list, cache-version script list, package-data list and first-load expected module list. Resolved by retaining both `editor-busy.js` and `preview-subtitles.js`, with EditorBusy before app startup, plus the current main validation logic. No provider contracts were invented. No secrets, accounts, copied videos, job state or model raw responses were staged.

## Spec review

- Editor busy retries remain opt-in to explicit pre-admission frame/compute/scan conflicts. Unknown 429s, generic batch/asset/worker gates and forced paid scans do not gain automatic retries. HTTP 202 body uncertainty is reconciled with GET, not a duplicate POST. Hidden-page/current-identity protections remain in place.
- Validation links still target visible material sequence/clip/field; focus waits for editor controls to unlock. Main's clone issue recomputation and edge cases are retained.
- Subtitle controls remain inside existing preview cards, horizontally aligned; one native subtitle track serves normal/fullscreen playback. Timeline is closeable/clickable. Output remains bound to master SHA/version, while recognition prefers a verified lightweight preview. Two materials run concurrently and the rest queue.
- The operator accepted small timestamp deviations for preview assistance after the four-material trial. This supersedes the earlier strict-accuracy concern, but does not imply full OCR recall or frame-accurate boundaries; occasional minor translation omissions remain recorded in the source acceptance report.
- Feature remains disabled by default until the production runtime is explicitly enabled with the existing Gemini credential file. No experimental prototype server is added to production.

No remaining merge blocker was found in either axis. The historical nonblocking retry diagnostic limitations and approximate subtitle timing remain documented.

## Complete and focused regressions

Integrated code including release-drain fixes: **1891 passed, 1 skipped, 10 existing multiprocessing/fork deprecation warnings**, exit 0, 478.67 seconds. Command: `PYTHONPATH=source /Users/matao/miniconda3/envs/py3.12/bin/python3.12 -m pytest source/tests -o addopts= -q --tb=short`.

After that full run, the only application change was adding the missing JavaScript resource to package-data. The new packaging regression and first-load/release cases passed (**10 tests**), and the actual wheel was rebuilt/audited. No business/runtime logic changed after the full run. All eight shipped JS modules pass `node --check`; `git diff --check` passes. Linux image/build validation is recorded separately in the release preparation manifest.

Private full-suite command/exit report: `full-tests-result.json`; full output: `full-tests.log`. Package audit: `package-audit.json`. Wheel SHA256: `b26a5c22827e223e289603e12eff0c1ecc8610284b06d18e50acdb12eb1c6cb0`.

## Additional live local dev testing

Instance: `http://release-review.localhost:8822/home/#0d6fdddb468348ad92e0cbd8f5de1db5`, candidate worktree `release-20261030`. Private data/launcher details are in `source/.dev-state/release-review/` and the release-prep directory. PID 68828 is a historical snapshot; recheck before stopping. Four copied materials are deliberately marked not delivered in this isolated fixture so editor actions can be tested; production and 8821 records are unchanged.

Real Chromium against real HTTP handlers and actual FFmpeg/copied videos; no intercepted API responses:

- All four cached Chinese subtitle results loaded, 99/113/103/110 cues.
- Four separate compute-lock admission conflicts displayed retry state, then automatically recovered after the actual file lock was released.
- Four uncached exact-frame requests hit the real frame-window lock timeout, then recovered automatically. Eight actual HTTP 429 responses were observed in total.
- Rapid multi-position seeks settled on the latest frame on every material.
- Eight additional close/reopen rounds across four materials, including immediate reopen and two controlled visibility-change cancellation/resume scenarios, passed.
- No uncaught page JS errors, no paid scan POST accepted, no render/publish/material-fetch started. Risk scan/tracking and all external writes are disabled in this test instance.

Private evidence: `live-dev.log`, `live-dev-results.json`, `release-dev-final.png`. Browser was an independent headless test browser; user tabs were not changed.

## Production preparation boundary

Read-only production status showed two active jobs and no maintenance/phase blocker at the time of inspection. This is a point-in-time observation, not a promise that release can switch immediately. Existing Gemini key is present; subtitle-specific override and runtime section are absent. No production config, controller, image selection, data or service was changed during local integration/review. Publishing must use the updated controller, enable the reviewed runtime/env settings, pass the Linux build/guard checks, drain active work and retain the current production release for code-only rollback.
