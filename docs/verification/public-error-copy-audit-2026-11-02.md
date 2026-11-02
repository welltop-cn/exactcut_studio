# User-facing error copy audit — 2026-11-02

The audit follows errors from validators, HTTP handlers and durable task reports through the UI. It excludes command-line output and diagnostic logs from product copy; those intentionally retain technical evidence.

## Confirmed findings and changes

| Surface | Confirmed problem | Result |
| --- | --- | --- |
| Shared HTTP errors | Error responses could contain raw KeyError fields, command output, filesystem paths or Pydantic dumps; the unexpected-error branch printed Python exception class names. | `public_message` preserves readable Chinese business errors and maps model, missing-field, file and unknown errors to plain guidance. Response status, error codes, retry metadata, trace IDs and redacted detail remain available. |
| Request popups | `EditorBusy.error` appended `error_info.detail` to the primary message, reintroducing technical output even when title/action were already friendly. | Known guidance uses title/action; unknown guidance uses the plain error. Diagnostic detail is retained separately rather than concatenated into the toast. |
| Main progress banner | Runtime failure messages were displayed directly, alongside an already separate structured error panel. | The banner and progress message use the structured Chinese title/action. Details remain collapsed in the error panel. |
| Cutting/mosaic workspace | Catch handlers, frame hints, scan reports, batch failures and preview/tracking failures could display raw client or historical task errors. | Shared message formatting applies at the display boundary; selection, retries, task state and cancellation conditions remain unchanged. |
| Preview subtitles | Failed cached reports and request failures could display raw error text. | Failed subtitle status uses plain copy with a subtitle-specific fallback. |
| Production helper | A browser network exception could be concatenated into the assistant response. | Readable business errors are preserved; an unknown failure asks the user to retry in Chinese. |
| Login | Browser fetch failures appeared as English exception messages. | Network failures now say `登录未完成，请检查网络后重试`. |

Existing deliberate Chinese `CutError`, subtitle model/queue errors and timecode checks were inspected and retained. File paths and other raw evidence in redacted diagnostic details are intentional and are not shown as primary error text.

## Verification

New regressions reproduce the original dumps through authenticated HTTP requests and synthetic historical task records in Chromium. Browser tests cover request error formatting, retry contract preservation, runtime banners and collapsed details, cutting scan failures, mosaic cached failures, subtitle failures, production-helper network failures and login network failures. An 84-test focused run passed after adding the new shared-helper dependencies to two isolated Node test harnesses.

Complete suite result: **1929 passed, 1 skipped, zero failures/errors**, in 270.82 seconds across two isolated processes (788 passed; 1141 passed / 1 skipped). The skip is the existing `test_editor_oss_playback.py:49` mosaic-only comparison condition. The final file/connection error classification was then verified in **98 passing targeted tests**, ensuring a connection failure is not described as a missing file.

Complete suite logs, JUnit reports and file manifests are stored privately under `source/.dev-state/public-error-audit-20261030/`. All tests use independent fixtures; they do not mutate the user's dev project or production data.
