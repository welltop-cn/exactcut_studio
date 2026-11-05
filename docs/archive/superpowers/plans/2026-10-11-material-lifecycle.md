# Material Lifecycle Implementation Plan

> 历史归档（2026-11-04）。正文保留当时的方案或执行状态，不代表当前版本；日常操作见[文档入口](../../../README.md)。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Keep project/asset identity stable through edits, additions and logical deletions; publish paired immutable current media safely, preserve delivery history, and validate/release on the real Linux server.

**Architecture:** A managed parent owns one atomic authoritative media-state document. Isolated internal candidate/delivery batches reuse existing scheduling and side-effect guards; only fenced, verified immutable output can become current. Existing projects remain readable and are enrolled explicitly; no OSS deletion or object overwrite.

**Tech Stack:** Python 3.12, existing JSON/fsync/flock persistence, existing staged subprocess scheduler, vanilla JavaScript, oss2, pytest, Docker/release manager.

## Binding design and review

Read `docs/archive/material-media-lifecycle-design.md`, including R2 which supersedes ambiguous earlier details. Grok reviewed R1; R2 is under follow-up review. No implementation of managed business writes until R2 blockers are addressed. Acceptance matrix is `docs/archive/verification/material-lifecycle-server-acceptance-plan.md`.

Internal contracts to define in code and test before use:

```python
# draft.json for hidden execution containers only; immutable lineage:
media_internal = {
    'parent_batch_id': parent_id, 'asset_id': asset_id,
    'version_id': version_id, 'generation': generation,
    'kind': 'candidate',  # or 'delivery' with delivery_id
}
# Main parent document identifies its format independently of legacy schema v1:
state_header = {'schema_version': 1, 'document_type': 'material-media-state',
                'batch_id': parent_id, 'revision': 1}
```

No client can supply OSS keys or internal batch paths. Existing valid request IDs remain stable; new IDs are assigned by the service. All new public fields must be defined in server output and corresponding tests before frontend consumes them.

### Task 1: Finish independent OSS safety regression

Files: `source/exactcut/preview_oss.py`, `source/tests/test_preview_oss.py`.

- [x] Reproduce Linux timestamp-coalescing regression: source is rewritten to same length during PUT; snapshot must stay original and receipt must not be accepted.
- [x] Add deterministic test that fixes `_identity` while bytes change.
- [x] On cold upload verify `file_sha256(video) == digest` after PUT, retaining hot receipt path.
- [x] Run preview/backfill focused tests locally and real Linux validation container: 12 passed each.
- [x] Commit `4ac68a6`. Re-run full tests after lifecycle integration.

### Task 2: Implement managed state and integrate existing flows as one consistency unit

Files to create: `source/exactcut/material_versions.py` and focused `source/tests/test_material_versions.py` (split lifecycle IO/worker adapters only if module responsibility demands it).
Files to modify: `workflow.py`, `webserver.py`, `stage_results.py`, `result_store.py`, `preview_oss.py`, `config/default.json`, `web/app.js`, targeted HTTP/frontend tests. Inspect `batch_control.py`, `accounts.py`, `concurrency.py`, `publish.py` contracts; change them only where actual integration requires.

- [ ] Define and test atomic state/registry protocol, project lock outside deletable project directory, full draft+revision+generation+idempotency in one transaction; fsync parent directory after rename.

```python
# Required behavioral assertions (fixtures exercise the actual adapter):
assert saved['id'] == original['id']
assert saved['draft']['rows'][0]['request_id'] == original_asset_id
assert current_master_before.read_bytes() == current_master_after_failed_attempt.read_bytes()
assert current_preview_before.read_bytes() == current_preview_after_failed_attempt.read_bytes()
```

- [ ] Add fixture-driven tests for stale revision and replayed mutation with different payload; ensure both rejection paths happen before side effects. Add recovery test for interruption between registry and main state creation; missing/corrupt state with marker rejects legacy writes.
- [ ] Implement explicit enrollment of quiescent historical project, retain original local files and legacy delivery hashes. Preflight discrepancies and active/unknown execution block enrollment. New managed project activation follows existing central config/environment conventions; existing un-enrolled read paths remain compatible.
- [ ] Save/rework/add/delete/restore remain in parent. Existing asset IDs/sequence reserved; only new assets get new identity/sequence. Copy project allocates all new asset IDs. Mutating media inputs use append-only files. Full deleted rows retained as tombstones, no rmtree/unlink of business data.
- [ ] Confirm immutable input/version/outbox in one commit. Materialize deterministic internal candidate and staged fetch/render job, exclude internal candidates from sidebar/global naming accounting/public HTTP, preserve owner for fairness. Recover incomplete materialization from outbox.
- [ ] Fence actual stage attempt before worker start and current commit using verified job context. No job recovery may recycle a published directory. Forward pause/archive to scheduling/publication decisions; resume preserves data, deletion invalidates before cancellation.
- [ ] Generate candidates in their own directories using existing pipeline. Freeze master and matching preview to new parent version files; validate each file hash and source-master relation, upload/verify both private OSS receipts and commit current atomically. Upload/copy/hash outside project lock. Catch failures without changing previous current. Old/duplicate workers cannot write shared parent result shards.
- [ ] Route preview/master download/receipt persistence to parent current version. Bind approvals to version even if bytes are identical. Return previous current during candidate processing and failures; expose candidate progress independently.
- [ ] Create immutable delivery snapshot/container per delivery ID and version, atomically commit intent/outbox. Block any new delivery while older version is pending/unknown. Reuse existing publish guard and verification path; reject same filename/different bytes without overriding target or inventing undocumented naming conventions.
- [ ] Route/reject every legacy parent/child mutating entry: CLI produce/fetch/render/preview, public start/save/clone/publish/verify/delete, manual preview, historical backfill, results and recovery callbacks. No managed fallback based on missing gate/state.
- [ ] Update frontend editability/rework to keep project selected, preserve IDs, include revision/idempotency/version binding, display old current plus candidate status, expose logical deleted-item restoration, and release video requests on switch as already implemented.
- [ ] Add full scenario tests: older generation finishing late; attempts reordering; delete while running; restore not reviving task; crash windows; frozen files unchanged; paired media; upload retry; unknown delivery; historical migration; direct child HTTP rejection; legacy writer rejection; controls; sequence reservation; path/auth boundaries.
- [ ] Run focused tests as each behavior is implemented, then full suite using isolated temporary EXACTCUT_DATA_ROOT. Never point tests at production data.
- [ ] Spec review then quality review by separate reviewer agents. Fix all material findings and re-review. Submit resulting full code plus design and fixtures to Grok for final review.

### Task 3: Linux validation and release

Files: acceptance report under `docs/verification/`, evidence-only runbook update.

- [ ] Build clean committed source using `/srv/exactcut/bin/linux-ops build` with new release ID. Never reuse the failed or archived release directory.
- [ ] Run full Linux suite in release validation container. Exercise real filesystem/process/crash tests in isolated data root, including restart recovery and cross-process locking. No prod credentials in dev/staging.
- [ ] Read-only check bucket lifecycle for prefix and current production activity; record limitations on external cleanup ownership.
- [ ] Use release-manager publish/drain/health procedure only after design/code review and critical tests pass. Maintain compatible rollback for managed state; do not revert DB or allow old writer after enrollment.
- [ ] Test independent synthetic production project and admin/other-account boundaries. Execute acceptance matrix; actual external FBBM writes only against a known test target.
- [ ] Backfill historical private preview/master objects serially and resumably; do not render/delete/rewrite business metadata. Verify receipts/version consistency.
- [ ] Chrome acceptance: actual metadata/playback, signature renewal, cancel switching, 10 switches and 5 refreshes with/without OSS preload, attachment download hash. Redact URLs/cookies in evidence.
- [ ] Record exact release/commit/full test counts/real-server scenario outcomes/untested gaps. Keep current production on any unsafe blocker. Update troubleshooting runbook with verified reusable findings only.
