# Render acceleration implementation plan

Goal: Reduce short Hook compositing time without changing requested cuts, preset, CRF, audio, or frame count.

Architecture: Use a conservative fast path for normalized H.264 base videos: encode only the opening through a verified safe keyframe, copy the untouched tail, and preserve full original audio. Unsupported or unverifiable inputs use the existing full encode. Encoding resource configuration follows the existing central configuration mechanism.

Tech stack: Python 3.12, FFmpeg/ffprobe, pytest, existing Linux release manager.

- [ ] Task 1: Inspect renderer and implement short Hook splice in `source/exactcut/renderer.py` and a focused helper if needed. Add `source/tests/test_hook_splice.py` with actual FFmpeg fixtures. Verify decoded tail hashes, frame count, timestamps, audio preservation, non-keyframe Hook ending, and safe fallback. Run new tests and `test_hook_work_bound.py`, `test_concat_copy.py` using `/Users/matao/miniconda3/envs/py3.12/bin/python3.12 -m pytest` from source. Obtain spec and quality reviews.
- [ ] Task 2: Add top-level `media.encoder_threads` through central configuration, default four and strict integer range 1–8, without invalidating semantic base caches. Preserve decoder/filter bounds. Test default, configured eight, invalid values/section, non-FFmpeg pass-through and cache identity. Retain production four: static bounds are not a global CPU budget; dynamic allocation is deferred pending separate recovery and concurrent-throughput validation. Hook previews share render slots, so existing three slots nominally permit twelve encoder threads. Obtain spec and quality reviews.
- [ ] Task 3: Run full suite. Benchmark old and new Hook compositing on independent video copies in an isolated network-disabled container. Record exact frame/audio checks and timing. Finish existing production baseline acceptance and update verification evidence and reusable runbook findings.
- [ ] Task 4: Commit clean changes, build and test via Linux release manager, drain safely, publish with health checks. Validate production rendering without reusing material IDs or initiating FBBM delivery. Record release and results.
