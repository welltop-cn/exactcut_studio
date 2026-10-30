# Material validation feedback acceptance — 2026-10-30

Implemented locally; no production deployment or production data mutation.

## Behavior

- Validation uses each card's visible `seq`: list position 6 with sequence 8 reports `素材 8 · 片段 1`.
- Missing episode, invalid numeric components and reversed time have separate messages and field locations before source indexes are read. Equal start/end is valid for inclusive source frames, invalid for millisecond ranges.
- Managed and legacy detail responses retain `errors` and add transient `validation_issues`; no durable schema change.
- Invalid cards have a red border and pale red background, a count in the header, per-clip messages and accessible invalid fields. Closing the summary preserves inline errors.
- Summary buttons focus the correct field; failed save/confirm focuses the first issue. Basic corrected fields clear immediately while remaining issues stay visible. Source-dependent errors require saving to revalidate.
- Deleting earlier materials keeps the highlight attached to the same row and updates its positional target.

## Verification

From `source`, using `/Users/matao/miniconda3/envs/py3.12/bin/python3.12 -m pytest`:

```
tests/test_validation_locations.py tests/test_validation_feedback_ui.py
tests/test_source_frame_ui.py tests/test_source_frames.py tests/test_composition.py
tests/test_source_frame_timecode.py tests/test_source_frame_clone.py
tests/test_source_frame_legacy_compatibility.py tests/test_source_frame_retry.py
tests/test_material_versions.py tests/test_segment_effect_intake.py
tests/test_clip_text_ui.py tests/test_segment_effect_ui.py
```

Result: **175 passed** in 40.71s. Three Python multiprocessing fork deprecation warnings from managed lifecycle tests. The new backend regressions initially failed on ordinal numbering and missing field projection; browser regressions initially failed on absent highlighting and focus behavior.

Playwright rendered the synthetic six-material example at 1440 × 1000 and 390 × 844. Desktop screenshot inspected: visible material 8, two error messages, red episode/time controls and card header. Computed card border `rgb(188, 57, 57)`, background `rgb(255, 248, 247)`; mobile document width 390 equals viewport width 390, with no horizontal page overflow. Screenshots: `/tmp/exactcut-validation-card.png`, `/tmp/exactcut-validation-card-mobile.png`.

Production release health checks have not been run; no production release was performed. The final full-suite result is recorded below.

## Extended acceptance requested by user

Added coverage for multiple invalid materials and clips, deleting an invalid material or an earlier clip, sequence edits, correcting then reintroducing an error, invalid confirm actions, narrow viewports, and real authenticated HTTP/browser saves for both managed and legacy projects. The real browser tests correct and save both invalid fields, verify persisted empty error arrays, reload, and confirm the start button becomes available. All use isolated test data rather than the user's dev project.

Additional reproduced defects and fixes:

- A pasted episode such as `²` passed Python `isdigit()` but failed integer conversion. Decimal validation now returns a located episode error instead of throwing.
- An empty frame field could be interpreted as zero while checking time order, producing a second, unproven error. Frame order is compared only when all components are filled.
- Cloning a managed draft copied transient `validation_issues` into its persisted document. Clones now discard that projection and recompute locations and labels for the new project.

Existing required-frame assertions now check the specific field projection and actionable message. The isolated Node mutation-lock test declares the added focus helper dependency; full browser tests separately verify focus occurs after managed inputs are unlocked.

Final verification: **1770 passed, 1 skipped, zero failures/errors** across the complete test-file set, run as two isolated pytest processes in 237.82 seconds. Shard results: 976 passed / 1 skipped and 794 passed. The skip is the existing `test_editor_oss_playback.py:49` condition, `mosaic comparison only`; it is unrelated to material validation. A focused run covering the expanded validation tests, frame-text requirements and managed mutation-lock tests also passed **77 tests**.

Private complete logs, JUnit XML and exact test-file manifests are under `source/.dev-state/material-validation-20261030/full-final-*`. The earlier complete run exposed six outdated required-field text assertions and the Node fixture dependency; those were updated and the final suite rerun after the code fixes. A superseded intermediate run was stopped before the final full run.
