# Production diagnostic logging implementation plan

**Goal:** Diagnose the failures in Feishu feedback revision 4 using persistent, correlated server evidence.

**Architecture:** Standard-library JSONL writer in data_root/logs; one inter-process lock covers append/rotation/retention. Existing centralized JSON config supplies limits. Context variables associate HTTP requests and stage workers; durable jobs retain the originating trace ID. Explicit events avoid logging bodies, prompts, credentials or full external responses. Logging failure never changes task outcomes.

**Tech Stack:** Python, fcntl, contextvars, existing HTTP server and stage scheduler; no new runtime dependencies.

- [ ] Test redaction, bounded multi-process writes, rotation/retention, unwritable storage and request correlation before implementation.
- [ ] Add diagnostic writer/config: 14 days, 1 GiB total, 20 MiB files; private permissions; rotation independent of process lifetime; throttled fallback on stderr.
- [ ] HTTP: request start/end/status/duration/trace header, authentication outcomes and error stack locations; no bodies/query strings/cookies. Job transitions and throttled queue-block reasons, periodic worker/resource summary, recovery and child exit evidence.
- [ ] Pipeline: source cache and lock waits, per-episode transfer/idle/timeout, resolved clip ranges and segment QC, Hook length and configuration fingerprints, preview timings, AI retries/HTTP status, OSS/Feishu/FBBM timing and publication states.
- [ ] Verify focused failure cases and full regression in an isolated clean committed checkout. Preserve concurrent Hook/UI work.
- [ ] Publish through existing release manager; verify persistent JSONL after restart and real read-only HTTP requests. Document query recipes, limits, and reverse-proxy vs application diagnosis. No historical root-cause claims without evidence.
