# Friendly material validation copy — 2026-10-30

The editor previously rendered `str(pydantic.ValidationError)`, exposing model names, internal input dictionaries, English validation text and documentation URLs. A missing ending image now reads:

> 素材 2 · 结尾画面：请上传结尾图片，或将结尾画面改为“不添加”

The banner title is now `请检查以下内容，修改后再开始制作：`.

`intake.model_validation_issues` reads the actual Pydantic error entries without input values or documentation URLs. It preserves custom Chinese business messages and supplies plain Chinese guidance for ending duration, ending guide, mode, font size and material sequence errors; unknown model failures receive a neutral Chinese message rather than an exception dump. Managed and legacy project detail use the same projection, including composition endings.

Ending issues identify their section and field. The editor marks the upload, duration, guide or mode control invalid, places its message inside the ending section, and focuses that control when the summary is clicked or invalid content is saved. Changing mode refreshes the highlights after replacing the section DOM. Uploading the missing image or choosing no ending clears the missing-image issue immediately; the backend validates again on save.

Verification: **117 passed** across `test_validation_messages.py`, `test_ending_validation_ui.py`, `test_validation_http_ui.py`, `test_static_ending.py`, `test_validation_locations.py`, `test_validation_feedback_ui.py`, `test_segment_effect_intake.py`, `test_hook_typography.py`, `test_brand_overlay.py`, and `test_composition.py`. New regressions first reproduced the raw exception text and missing field highlighting. Authenticated browser tests cover both legacy and managed projects, both image upload and cancellation, invalid-save focus, save success and reload persistence. Rendering of the new concise banner was visually inspected at 1440px; screenshot `/tmp/exactcut-friendly-ending-error.png`.

All tests use isolated fixtures. No production deployment or production data mutation was performed.
