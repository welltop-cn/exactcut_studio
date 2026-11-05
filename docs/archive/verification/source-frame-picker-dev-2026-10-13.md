# Source frame picker development acceptance — 2026-10-13

> 历史归档（2026-11-04）。正文保留当时的方案或执行状态，不代表当前版本；日常操作见[文档入口](../../README.md)。

## Delivery and isolation

- Worktree: `~/.config/superpowers/worktrees/exact-cut-fbbm-handoff/source-frame-picker`, branch `feat/source-frame-picker`, base `2217055`. Uncommitted; no merge, release, production mutation or shared dev restart.
- Live new frame-format demo: `http://localhost:8799/home/#95f99b49174e4cbb80dbbe381abde05c`.
- Original local millisecond copy: `bb380fdda28544e98ed8839a2dfa1972`. Its full draft was compared with the original copied draft and remains exactly equal. Existing projects are not migrated.
- Private launcher `/tmp/exactcut-source-picker-dev-20261013/start.py`; independent data sibling `data`. Worker, source fetching, AI, publishing and external writes disabled. Only this session's verified 8799 process was restarted to load backend changes. Recheck current listener before later actions.
- User explicitly requested readonly copies from project `1cde1c2787f64d149c97c8fd948e4e6a`: drama101619, two materials, seven clips, EP1/3/4/5/7 and referenced ending image. No production data was changed. New demo separately interprets copied numeric fields as frames per user clarification; original local and production projects remain unchanged.

## Final interaction

- Two static images show first and last retained source frame. Two independent thin vertical lines with small start/end grips select each endpoint directly; no card-selection prerequisite.
- Shaded retained interval, time ruler, independent previous/next buttons, focused-line arrow key stepping, optional selected-range playback, and visible Cancel/Apply.
- Dragging start updated only left preview; dragging end preserved left and updated right in real browser acceptance. Cancel/reopen preserved original configuration.
- Overlapping lines retain separately reachable grips. DOM hit testing and actual pointer regression cover focusing start then dragging end at the same initial frame.

## Time contract

- New manual UI creation explicitly sends `time_format: source_frames_v1`; draft has `editor_time_format`, clips retain `time_format` with `start/end_m,s,f`. Older missing-format clips retain millisecond fields and behavior. Composition and existing imported formats remain unchanged.
- `f` is zero-based frame ordinal in the actual source presentation second. It is not a fixed-fps assumption or SMPTE nominal-rate timecode. Inclusive end maps to the next source frame boundary (or source duration) for the existing millisecond renderer.
- No source/index: draft can be saved and episodes prefetched; execution is blocked until an actual index is prepared and validated. UI directs user to open the picker and save/validate. Pasted text uses an explicit frame contract for new projects and the original millisecond contract for old projects. Standard frame lines parse locally; complex natural language uses the existing AI transport with a separate frame schema. Ambiguous millisecond/decimal/SMPTE input is rejected rather than converted.
- Backend ignores supplied timeline for explicit frame fields. Current fingerprint/index checked; source replacement requires a current index. There is no permanent source-identity binding in saved drafts, consistent with existing rendering behavior.

## Evidence and review

- Real-FFmpeg tests compare indexed PNGs against sequential decode for noninteger B-frame video, VFR, nonzero origin and actual rendering bounds. Frame responses use immutable bytes and atomic cache publication.
- Real copied EP4 frames31/1500 and EP1 frames61/3480 matched sequential RGB decoding exactly. Evidence: private `real-frame-check.json`. All five real source indexes prepared; new demo save returned no errors.
- Review fixes: row identity and navigation fences, per-end callback generations, Apply freeze/snapshot, retry reuse, cache write/eviction race, empty retained intervals, legacy `clips:null`, binary-search bucket lookup, overlapping drag hit regions. Independent final review found no blockers.
- Backend focused regression: 118 passed with three existing multiprocessing fork deprecation warnings.
- Full regression: 1212 passed in223.92s. Final overlapping-handle change was additionally verified in the full current UI module: 23 passed in12.33s. JS syntax and diff checks pass.
- Live preview remains available at user request, with the new frame demo picker open. No production release performed.


## Downstream production-flow acceptance

- New managed integration goes frame draft → actual index → save → enroll/confirm → child confirmed millisecond manifest → real cut/Hook/still ending/QC. Child has no picker index and an intentionally invalid editable draft; execution still correctly uses its immutable confirmed manifest.
- Synthetic 29.97fps B-frame source: base54 frames match source19–72 byte-for-byte; Hook final story frame matches source72 after the same color conversion; ending frame matches selected last frame; final84 frames, audible story, silent ending and full decode pass.
- Actual101619 EP4 copy, normal1080×1920/30fps/medium/CRF18: two2s clips +1s still ending, Hook, QC and preview executed. Frame and equivalent legacy millisecond drafts compile to identical manifests. After the EOF fix, output150 frames, video/audio each5.000s, QC allowed and preview generated. Private output/receipt: `/tmp/exactcut-frame-render-acceptance-20261013/acceptance.json`.
- This found a pre-existing EOF defect in repeated fps filtering after overlay:45/120-frame sources yielded44/119, and fractional-fps output could repeat the penultimate frame. TDD captured both count and exact tail-pixel failures. Only normalized inputs skip redundant fps; unnormalized handling remains unchanged. Full-hook/splice/integration focused18 tests pass; independent review found no blockers.
- Only dedicated8799 was restarted for this fix, after verifying its command and no active jobs. Shared services and production untouched. Final complete regression after this correction: **1216 passed in224.69s**. Dedicated dev health returned200/ok in development mode; changes remain uncommitted and unreleased.

## Existing projects: no migration

- Missing format markers continue to mean milliseconds. Existing `019` stays19ms, not19frames; existing projects, copied projects, added rows and clips retain this format.
- Additional lifecycle regression `test_source_frame_legacy_compatibility.py`: **5 passed**. It forbids all frame-index entry points while exercising legacy/managed save, append, clone, failed retry and completed-output reuse. Frozen confirmations and existing output/result bytes remain unchanged. The completed-output test uses the existing integrity fixture, not a new real encode.
- Existing confirmed child manifests remain schema1 milliseconds. New frame inputs are resolved before confirmation, so production stages do not consume the new editable notation.
- No historical data conversion or bulk rewrite is planned. User asked whether compatibility scope can be reduced; retain the old millisecond path without adding migration or old-project frame conversion.
- Code downgrade is a separate outstanding release constraint: read-only execution of the HEAD intake compiler shows that an old writer drops `time_format` and `_f` fields, replacing missing milliseconds with zero. Do not roll back to a pre-frame writer after creating frame drafts. Existing schema/stage/media release checks do not enforce this capability. Release remains withheld; no deployment-manager edits have been made.
- Actual Grok review session `8075840d-e4c3-4ff5-a4a6-8e316b200b9e` completed independent review and follow-up. Initial broad review hit its tool-turn limit; the resumed session issued a formal finding, then independently reviewed the fix. Public verdicts are retained privately as `grok-review-2-verdict.md` and `grok-review-3-verdict.md` under the dedicated dev directory.

## Grok finding, correction and agreement

- Grok identified one functional blocker: `Handler.hook_preview` called `validate_draft` without the batch source path, so even prepared frame indexes could not be found. Fixed the call to pass `str(workflow.folder(batch_id) / 'source.xlsx')`, matching existing detail/confirm validation.
- New `test_source_frame_hook_preview.py` reproduced HTTP400 before the fix for ordinary and managed frame drafts. Both now return200. Real29.97fps source/index/save and HTTP are used; only external Hook compute is replaced, and the test checks it receives the actual633ms start for frame19 rather than19ms.
- Parent verification: **26 passed in12.48s** across new preview, legacy compatibility, phase2 preview, managed editor and preview capacity modules. Grok independently reran the two new preview tests; both passed. The prior1216 full-suite result predates this one-line fix; no newer full-suite result is claimed.
- Final Grok verdict: “同意这些结论。功能层面不再有阻断。这是开发验收通过，不是上线批准。” We agree to retain legacy milliseconds without migration, resolve new frames to the existing confirmed manifest, and leave incompatible downgrade prevention as an explicit unfinished release prerequisite. No production release, shared-service restart or deployment-manager edit occurred.


## Full flow omission audit and text-entry correction

The first frame-picker acceptance missed pasted text and is superseded by this audit. User explicitly requested verification of all input paths instead of discovering gaps during use.

- Fixed pasted text entry, format-specific instructions/examples, explicit API format, append/replace/undo, empty frame-placeholder removal, and rejecting responses in the wrong time format. Actual frame fields remain `_f`; no fixed fps conversion occurs in text parsing. Legacy AI millisecond schema/prompt stays unchanged.
- Added strict frame-field completeness at source resolution: missing/blank minute, second or frame fields cannot silently become zero. Standard two-part pasted times explicitly supply frame0 under the dialog's stated convention.
- Found and fixed stale video playback after picker source reload; released old video source before loading fresh metadata.
- Found and fixed confirmed legacy failure retry depending on a disposable frame index. Existing confirmation is authoritative; first confirmation still validates the editable draft.
- Updated production helper's authorized context and knowledge to distinguish frame/new and millisecond/old projects. Corrected obsolete new-project/Excel advice while retaining old imported timeline guidance.
- Final complete local regression: **1259 passed, 6 existing multiprocessing fork deprecation warnings, 236.78s**. This includes both manual and pasted-text real FFmpeg production integration, exact endpoint/tail pixels, final frames/audio/ending, lifecycle, auth, UI interaction and race regressions. An earlier in-progress suite was intentionally stopped while fixes were still being made; it is not counted as final acceptance.
- Real browser acceptance on dedicated8799: new isolated project `39155ae063e649ea9f4eb8a11b083fa0`, drama101619 copied source. Pasted `第4集 00:10:019-00:12:018` plus `EP4 00:25:005 至 00:27:004`; two frame rows filled, first picker showed60frames/2s. Advanced start by one frame, applied, saved with no errors and reloaded: `00:10:020`–`00:12:018` persisted, picker59frames/1.967s. Screenshot `/tmp/exactcut-source-picker-dev-20261013/frame-text-browser-acceptance.png`.
- Only8799 restarted after checking ownership and no active jobs. Browser-created acceptance data is local; user-created drafts and original old-format demo were not changed. Shared dev and production untouched.
- Local dev intentionally disables external AI/fetch/publish. Explicit frame-line paste and cached-source picker were exercised live; complex AI transport is covered with a mocked provider response, not a live AI request. Full rendering/confirmation runs use isolated integration data, not the dev worker (disabled).
- Actual Grok follow-up review completed for both the text-entry increment and the three audit fixes. It independently reran24 parser tests, inspected UI/frame completeness and then player/retry/helper paths; both final verdicts report no remaining functional blockers for development acceptance. Public verdicts are retained as grok-review-4-verdict.md and grok-review-5-verdict.md in the private dev directory. Grok did not claim to rerun the1259-test suite; that was the parent run. Incompatible code-downgrade protection remains an unfinished release prerequisite. No release approval is claimed.
