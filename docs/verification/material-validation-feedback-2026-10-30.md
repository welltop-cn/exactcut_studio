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

Full repository suite and production release health checks have not been run; they remain release requirements.
