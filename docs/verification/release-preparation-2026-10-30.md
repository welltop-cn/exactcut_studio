# 2026-10-30 release preparation and publication checklist

This is a prepared release procedure, not an instruction that production has been switched. Scope: current main validation fixes, editor resource-busy recovery, preview-only Chinese subtitles with a two-material FIFO worker pool, subtitle release-drain guard, and complete browser package assets. The approved small subtitle timing deviations remain acceptable for preview assistance.

## Pinned production baseline

Read-only inspection on October 30 returned:

- current release: `us-east-20261028-originals-7960491`;
- commit: `79604912a71d5224a42fa6717c8fb8ae0b0a6ffd`;
- current immutable image: `sha256:3019f65c2fe54d39ded9d83c9cb78430431ced08041ce31eefaed9d4af7e14cd`;
- previous: `us-east-20261028-editor-oss-33b0d1b`;
- two active jobs, no phase blocker and no maintenance at inspection. Recheck immediately before actual switching; never force-stop jobs.

Production root remains `/srv/exactcut`; public entry remains https://autocut.creativefitting.ai/home/ . Existing account/data/storage/OSS/Feishu/FBBM configuration must be preserved. Schema remains version 1.

## Candidate and artifacts

The final commit, release ID, artifact paths and SHA256 values are recorded in the private `release-manifest.json` under `/Users/matao/.config/superpowers/release-prep/exactcut-20261030-1802/`. Source is built from a clean committed checkout, not the main working directory's unrelated historical untracked notes. Git bundle and source archive contain committed code/docs only. Experiment demo modules and all local credentials/video/task caches are excluded.

Local evidence: [integration review](reviewed-integration-2026-10-30.md). Dedicated dev entry: http://release-review.localhost:8822/home/#0d6fdddb468348ad92e0cbd8f5de1db5 . The latter is an independent local copy with deliveries removed for editor testing; it is not production delivery evidence.

Server preparation uses an isolated engineering clone under `/srv/exactcut/preparation/review-20261030/`. Use the candidate's reviewed `source/deploy/linux_ops.py` to build the immutable candidate and run the standard isolated Linux suite. Building/preparing is separate from `publish`; no production config or service is changed by preparing the clone and building the image. The release manifest/build record must include exact source commit, Linux test result, immutable image ID and sealed release metadata. A build failure blocks publication.

## Required activation changes — apply only with publication

The feature is safely disabled by default. The existing production env already has a Gemini key; do not create/copy a second secret file or print the key.

Merge this section into existing `config/runtime.json` through the established JSON configuration path, preserving all other keys:

```json
{"preview_subtitles":{"enabled":true,"max_active_tasks":2}}
```

Set this nonsecret override in existing `config/production.env` (absolute path inside the already-mounted container):

```dotenv
EXACTCUT_PREVIEW_SUBTITLE_ENV=/run/exactcut/production.env
```

Preserve file mode 0600 and existing `EXACTCUT_CUT_SCAN_ENV`, keys and feature flags. No Docker dependency changes are required; the pinned requirements already include google-genai 2.21.0. Both old application and unchanged rollback configuration tolerate the extra disabled-by-code subtitle section/env override, but retain precise previous-config backups and compare actual differences before any restoration.

The installed production release controller must also gain `f779a37`'s subtitle task-drain logic before subtitle tasks are allowed. Back up the existing controller, verify the candidate controller's hash, and install the reviewed `source/deploy/linux_ops.py` through the existing controller maintenance path. Until that is done, use the candidate's versioned controller directly for any preparation checks; do not rely on the old controller to see subtitle work during later rollbacks. Never use legacy `deploy/install.py` or restart services manually.

## Publication and rollback

After explicit production publication authorization and successful candidate build:

1. Read current status again and compare current release with this baseline. Preserve newer changes if another release occurred.
2. Back up and apply only the reviewed controller/config changes above. Retain source/config/controller hashes in the deployment evidence.
3. Use the updated manager's `check-switch <candidate-release-id>`. It must report allowed=true with no active work/phase blockers; if work is active, wait for normal completion. Running subtitle calls block switching; queued subtitles remain pending under maintenance and resume under the new code.
4. Publish via `/srv/exactcut/bin/linux-ops --root /srv/exactcut publish <candidate-release-id>`. The manager owns the maintenance barrier, drain, immutable image selection and health-failure recovery.
5. Check local/public health for exact release, production environment, worker_alive=true and maintenance=false. Verify authenticated versioned JS URLs include both editor-busy.js and preview-subtitles.js and no asset 404/500. Reopen the page/editor to avoid old JS.
6. Verify existing previews and read-only download hashes. Generate subtitles only on an explicitly chosen test/approved material; do not start risk scans, replace masters, upload or trigger FBBM/Feishu writes as a deployment smoke test.
7. Record actual current/previous release, image ID, code/config/controller differences and smoke results in a new publication record.

If publication health fails, retain the manager's automatic code recovery and inspect its receipt; do not restore an old database. For an operator-requested rollback after a successful publication, use the updated manager and the recorded current predecessor (baseline shown above only if unchanged), respecting task drain and compatibility gates. Preserve pending subtitle files, current business data and paid-call interruption records; never auto-repeat interrupted paid calls. Restore a feature toggle/env override only after confirming no newer config edits would be overwritten.

The last local gate is a clean committed source archive plus the recorded complete/focused tests, wheel resource audit and live-dev results. Final production activation remains a separate action from this preparation task.
