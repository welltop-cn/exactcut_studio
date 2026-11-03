# OSS delivery verification repair

Goal: reuse copied OSS masters, preserve failure evidence, and show stopped deliveries truthfully.
Architecture: keep the existing publication intent protocol and schema 1. Use HEAD metadata for SHA verification; distinguish mutation-blocking deliveries from actively executing deliveries. Recover only this incident after verifying the durable pre-Feishu failure and remote object identity.

- [x] Reproduce copy/reuse failure with get_object_meta omitting custom metadata; retain head_object SHA and size checks. Tests: test_preview_oss.py.
- [x] Reproduce finish_job overwriting specific publication error; preserve existing evidence, generic interruption only if none exists. Tests: test_concurrency_recovery.py.
- [x] Reproduce managed unverified/publish_failed banner; distinguish active delivery, unverified, and failed statuses; add readable frontend labels and appropriate error guidance. Tests: test_material_versions.py.
- [x] Run targeted and complete pytest suites using ~/miniconda3/envs/py3.12/bin/python3.12. Commit clean revision.
- [x] Verify and back up incident metadata; inspect release guards and recovery path. Build/test Linux using existing linux_ops manager; release only after guards pass.
- [x] Resume only confirmed pre-Feishu deliveries using existing OSS objects and original approved hashes. Verify FBBM outcomes, page status, and record evidence/runbook. Never delete OSS objects or replay uncertain downstream writes.
