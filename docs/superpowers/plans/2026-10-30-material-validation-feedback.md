# Material validation feedback implementation plan

**Goal:** Use the visible material sequence in validation messages and locate invalid cards, clips and fields.

**Architecture:** Keep the existing string `errors` contract and add a transient `validation_issues` projection with zero-based row/clip locations and field names. Both managed and legacy detail responses expose it; no durable schema change. The editor renders links, card summaries, inline messages and accessible invalid fields.

**Steps:**
- [x] Add regression cases for list position 6 / visible sequence 8, missing episode, reversed time, and browser navigation and correction feedback; run to establish failure.
- [x] Add detailed clip checks before source indexing and preserve locations for source-index errors. Use visible sequence in all per-row validation messages.
- [x] Expose structured issues in both detail paths; render red cards and fields, clickable summary messages, and first-error focus after saving.
- [x] Run focused backend and browser regressions, inspect the final diff, and record acceptance evidence. Commit only the task files.

**Verification:** Use `/Users/matao/miniconda3/envs/py3.12/bin/python3.12 -m pytest` from `source`, including new validation tests, source frame UI and source frame regressions. Local synthetic fixtures only; production deployment is a separate release operation.
