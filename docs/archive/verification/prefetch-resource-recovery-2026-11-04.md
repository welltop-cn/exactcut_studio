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
3. Recovery also recognizes output-free resource waits written with the previous fingerprint algorithm. This fallback requires version 1, waiting_resource/75, a valid seal, the same fingerprint recorded in the job, a finite positive retry time, and a current plan whose drama and episodes authorize the job. The old plan hash must still match. It never accepts old done results, publications or artifacts. Missing/malformed plans and changed job scope are rejected. Requeue clears the old input fingerprint; the next attempt always uses the new scoped fingerprint. Old evidence whose plan has already changed remains invalid.

No durable fields, schema version, dependencies or concurrency settings are added. The queue-of-ten admission limitation in the deployed release is already removed on the `main` base and is not reimplemented here.

## Regression evidence

The initial regression drives the actual sources HTTP API, the stage child wrapper, resource deferral evidence and scheduler completion. The unmodified base marks the task `failed`; after the fingerprint fix alone, it requeues but incorrectly changes the draft runtime to `queued`. Both assertions pass after the two targeted changes.

Twenty-seven new cases cover expanding and shrinking the desired episode set through normal child completion and restart recovery, cancelling then resuming as skipped without downloads, and rejecting changed job project/episode arguments. They also cover previous-version waiting records through both completion paths, refusing old success/artifact/publication or corrupt evidence, missing/malformed plans, and an actual `start_batch` confirmation while prefetch waits. The confirmation case verifies that the resumed prefetch skips downloading and preserves the confirmed manifest and production runtime byte for byte.

The initial cross-version test failed on `4c1b1ec`: an unchanged plan plus valid previous-version wait became `interrupted`. Both reviewers confirmed this regression before the compatibility fix; the bounded legacy fallback fixes it without accepting completed outputs.

Command, from `source` with the configured Python 3.12:

```text
python3.12 -m pytest -m '' -ra --junitxml=.dev-state/prefetch-recovery-verification/review-regression.xml tests/test_prefetch_resource_recovery.py tests/test_source_prefetch.py tests/test_concurrency_recovery.py tests/test_concurrency.py tests/test_phase1_recovery.py tests/test_frame_contention.py tests/test_source_frame_timecode.py tests/test_intra_project_prefetch.py tests/test_managed_scheduler_verification.py tests/test_round1_evidence.py tests/test_round2_evidence.py
```

Result after the review fixes: **171 passed in 47.35 seconds**, with three multiprocessing fork deprecation warnings from existing managed-verification tests. The explicit empty marker selector includes relevant integration/UI cases normally excluded by the repository default. Tests use independent fixtures; no production credentials or media were copied into development.

The final JUnit evidence is `source/.dev-state/prefetch-recovery-verification/review-regression.xml`. Full repository regression and release checks remain required before a future production release.

## Independent review

The user-selected D3 profile independently ran the original 115-case suite and 36 surrounding evidence/managed-verification tests. Initial review missed the cross-version fingerprint regression; the main agent reproduced it and D3 confirmed the failing test. Both agreed on the bounded, sealed resource-wait compatibility path and the additional confirmation boundary test. D3 also corrected its initial claims: ordinary `verified` does not generally check a seal, and `current_managed_verification` does not guard ordinary prefetch callbacks. The new compatibility path explicitly checks the seal itself. `episodes=None` remains distinct from an empty list rather than being silently normalized.

Final implementation review is pending D3 confirmation; no merge or release is authorized by this record.
