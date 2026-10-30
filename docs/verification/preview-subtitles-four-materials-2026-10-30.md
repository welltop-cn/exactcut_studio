# Four-material preview subtitle acceptance — 2026-10-30

Local worktree: `feat/preview-subtitles`, uncommitted, not deployed. Operator requested real copies of project `0d6fdddb468348ad92e0cbd8f5de1db5`, fixes for three failed materials, accuracy checks, and a real two-material concurrency trial. All work in this continuation was performed by the coordinating agent.

Entry: http://subtitle-preview.localhost:8821/home/#0d6fdddb468348ad92e0cbd8f5de1db5 . Dedicated development service PID 61192 at verification; recheck PID before any future action. Existing 8793/8800/8801/8810/8820 services were not restarted.

## Failure evidence and implementation

- Original source was the 160–197 MB master despite available 9.5–10.8 MB previews. Fixed-size/CRF proxies could exceed the 19,000,000-byte whole-request guard after base64. Recognition now copies and verifies the current preview, retaining master SHA/version authority. The scale filter never enlarges a smaller input, and proxy encoding uses the configured model sample rate (4 fps).
- A real response contained `start_ms=33500,end_ms=3500`. No guessed timestamp correction is applied. Invalid JSON/text/times trigger one additional request for that segment. Provider exceptions are not blindly retried. Oversized local segments split into contiguous halves before API admission; no under-1-second child is created by splitting.
- Valid segments persist privately, keyed by exact recognition-input SHA, complete duration and recognition policy. Explicit retries reuse validated segments. Failed tasks still expose no incomplete timeline.
- Two durable active slots plus FIFO pending queue, default maximum 2, centralized configuration 1–2. The dispatcher retains its cross-process lease until all workers finish. Existing schema-version-1 state remains readable via the primary `active` descriptor, with optional `active_extra` for the second slot.
- Initial real concurrent trial revealed an old `active.lock` still restricting execution to one material. Added a regression exercising actual `execute` for two assets in one managed batch, reproduced its failure, and removed the obsolete global execution lock. Per-task worker locks and shared batch leases remain.

## Real final results

| Material | Duration | Cues | Processing seconds | Requests | Largest request budget estimate |
|---|---:|---:|---:|---:|---:|
| R0d6fdddb | 206.250 s | 99 | 137.900 | 5, including one validation retry | 6,525,116 bytes |
| Rb4ea94a929654f41ab65b255dc64ece8 | 213.321 s | 113 | 133.056 | 5, including one validation retry | 6,711,436 bytes |
| Rf63c4d459b7b484b9a201f21652eb81c | 200.667 s | 103 | 108.314 | 4 | 6,452,112 bytes |
| R79f9b018e23f4a7e9725da45219485d8 | 195.953 s | 110 | 106.101 | 4 | 6,304,928 bytes |

Final two materials used genuine new model requests in overlapping workers, not completed-cache hits. Observed durable active count reached 2, both progressed through four segments, and their combined wall interval was 108.314 seconds. Their earlier serial trial took 85.816 + 100.967 = 186.783 seconds; this comparison is one trial, not a throughput guarantee. The successful serial outputs and unsuccessful first concurrency trial were archived privately before re-execution. All four final reports are completed; queue active/pending/extra slots are empty.

## Accuracy evidence and limits

Independent local Tesseract OCR was run at every final cue midpoint (425 points) to help locate suspect samples. OCR on small outlined text over moving backgrounds was unreliable; its match rate is NOT a subtitle accuracy metric. No OCR dependency was added to the application.

Manually inspected 12 distributed cue midpoints per final material (48 samples), including the beginning, middle and ending. 47 displayed the expected English subtitle at the midpoint. One sample in material 1 (`Oh Chris`, cue 0) fell into a real blank gap: the text is visible through approximately 0.75 s and absent by 1.0 s, but Gemini returned 0–1.75 s. This is a measured remaining timing defect, not a passed boundary check. Sampled translations generally preserve dialogue meaning, but material 3's `vampire power for a short while` omitted the duration qualifier in Chinese. Proper-name transliterations also vary between segments/materials. Full subtitle recall, every translation, and frame-accurate boundaries are NOT established.

Additional private one-segment experiments strengthened the gap/translation instructions and then added visible timecodes. They still returned the first cue end at 1.50 s and 1.75 s respectively. These ineffective experiments were not integrated, and no sample-specific manual corrections were injected. The current implementation is suitable for further preview-assistance evaluation; it does not yet meet a strict requirement of completely accurate subtitles.

## UI and preview-only checks

Fresh browser tab: all four controls resolved to cached counts 99/113/103/110. Each player had exactly one showing native track, including after loadedmetadata/reload and a toggle off/on cycle. Four midpoint seeks selected the expected Chinese active cue. Timeline opened with 99 entries; selecting `00:24.75` closed the dialog and selected `克里斯` at 24.764551 s. The owned verification tab was closed afterward; user tabs were not edited.

All 8 master/preview SHA-256 hashes still match the copied version records. Recognition uses separate temporary files; source master, preview, media-state, export and delivery data are not modified. Subtitles remain browser-only and are not burned into downloaded or uploaded video.

## Regression checks and private evidence

56 focused tests passed across concurrency, queue/recovery/auth, source selection, real FFmpeg no-upscale/4-fps behavior, size splitting, one bounded validation retry, segment checkpoint/source isolation, HTTP and native-track UI. `git diff --check` passed. No full-repository release suite or deployment was performed.

Private evidence root: `source/.dev-state/preview-subtitles/`:

- `parallel-evidence.json`, `four-material-acceptance.json`, `accept-parallel-fixed.log`;
- `browser-final.json`, `accuracy/ocr-results.json`, four `accuracy/*-samples.jpg`, `accuracy/first-boundary.jpg`;
- model raw responses and validated segments in `data/.web-state/batches/<batch>/preview-subtitles/<task>/`;
- `serial-acceptance/` and `parallel-first-pass/` retain previous trial outputs;
- `gap-experiment/`, `timestamp-experiment/` retain unsuccessful timing experiments.

No secrets or raw media evidence are tracked in Git.
