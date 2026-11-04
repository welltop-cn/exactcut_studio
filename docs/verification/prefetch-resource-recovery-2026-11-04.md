# Prefetch plan edits and resource-wait recovery — 2026-11-04

## Production recovery without a release

Project `8aeb6eb7293e42acbca211b80c3baca1` (101689) had a failed prefetch task whose stage evidence actually said `waiting_resource`, exit 75. EP2's video was present without a frame index; EP3 was absent. The stage input fingerprint matched the earlier EP1–EP3 desired plan, while the editable project now requested EP1–EP6. Normal retry returned HTTP 200 without scheduling a task because the deployed version still has the old total-queue limit of ten.

After user authorization, the original failed prefetch job was backed up and restored to queued. Stale executor fields and the old input fingerprint were removed; the attempt number was retained so the existing worker started attempt 2 and generated fresh evidence. The normal scheduler downloaded/indexed the needed files and finished with exit 0. No code release, service restart or rendering submission was performed. At recovery verification, all six episode indexes were ready, validation returned no errors, and the editable draft remained revision 12 with the same state-file SHA as before the operation.

The user subsequently saved/submitted the project; a later authenticated GET showed revision 15, status queued and no validation errors. This later user edit changed the draft SHA, so the recovery-time preservation check must not be presented as a guarantee that the draft never changed throughout the session.

Project `0d5153efa85e4a1cbce617f4309c55b2` (101693) was a different case: EP7 was still undergoing normal frame indexing. Its existing task completed on its own with exit 0 and validation became empty. No state repair was performed for that project; a later authenticated GET showed revision 11, queued, errors empty.

## Development branch

- Branch: `fix/prefetch-deferred-plan-recovery`.
- Base: `main` at `9002646`.
- Worktree: `/Users/matao/code/exact-cut-prefetch-recovery`.
- Other workspace changes are left untouched; this branch is not merged or deployed.

## Minimal fix

1. Prefetch stage fingerprints bind the job's existing immutable `project` and `episodes` arguments. The editable global `source-prefetch.json` no longer invalidates that job's resource-wait evidence. The child still reads the current desired plan before every episode and skips cancelled/unreferenced work; consumers still validate source signatures and frame indexes.
2. Requeuing a prefetch resource wait does not rewrite the project's runtime to production `queued`. It remains an editable draft. Normal production fetch/render/upload/poll fingerprints and runtime transitions are unchanged.

No durable fields, schema version, dependencies or concurrency settings are added. The queue-of-ten admission limitation in the deployed release is already removed on the `main` base and is not reimplemented here.

## Regression evidence

The initial regression drives the actual sources HTTP API, the stage child wrapper, resource deferral evidence and scheduler completion. The unmodified base marks the task `failed`; after the fingerprint fix alone, it requeues but incorrectly changes the draft runtime to `queued`. Both assertions pass after the two targeted changes.

Seven new cases cover expanding and shrinking the desired episode set through normal child completion and restart recovery, cancelling then resuming as skipped without downloads, and rejecting changed job project/episode arguments.

Command, from `source` with the configured Python 3.12:

```text
python3.12 -m pytest -m '' tests/test_prefetch_resource_recovery.py tests/test_source_prefetch.py tests/test_concurrency_recovery.py tests/test_concurrency.py tests/test_phase1_recovery.py tests/test_frame_contention.py tests/test_source_frame_timecode.py
```

Result: **115 passed in 25.49 seconds**. The explicit empty marker selector includes relevant integration/UI cases normally excluded by the repository default. Tests use independent fixtures; no production credentials or media were copied into development.

Private logs/JUnit from the earlier 113-case run are in `source/.dev-state/prefetch-recovery-verification/`; the final 115-case rerun includes two additional restart-recovery parameters and its result is recorded above. Full repository regression and release checks remain required before a future production release.
