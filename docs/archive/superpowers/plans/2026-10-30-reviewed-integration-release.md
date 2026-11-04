# 2026-10-30 Reviewed integration and release preparation

> 历史归档（2026-11-04）。正文保留当时的方案或执行状态，不代表当前版本；日常操作见[文档入口](../../../README.md)。

> **For agentic workers:** Follow this plan inline. Operator asked this coordinator to inspect, commit, merge into main and prepare production release. Preserve existing branches, worktrees and private dev data. Do not publish automatically: this request prepares the release.

**Goal:** Integrate the October 29–30 approved work into a verified main revision, preserve deferred/experimental work, and produce an immutable source artifact plus deployment/rollback instructions.

**Architecture:** Use isolated worktree `release-20261030`, starting at main `db4c684`. Merge `fix/editor-busy-retry` and a focused commit of `feat/preview-subtitles`. Keep version-1 durable data and existing release-manager flow. Production remains unchanged during preparation.

**Tech Stack:** Python 3.12, Node/browser tests, FFmpeg, existing Linux Docker release manager.

- [x] Pin date window October 29–30 Asia/Shanghai; main baseline before window `875c237`, current main `db4c684`.
- [x] Inventory all worktree records (including the missing/prunable entry), branches and six historical stashes. Snapshot dirty tracked patches and nonignored untracked files outside Git at `~/.config/superpowers/release-prep/exactcut-20261030-1802/`.
- [ ] Commit subtitle production implementation, tests and real acceptance documents in its existing feature branch. Keep 8821 service/data intact.
- [ ] Merge resource-busy recovery and subtitle implementation into isolated candidate. Resolve script/package inventories by preserving both features and main validation feedback.
- [ ] Review Standards (source/AGENTS.md, existing architecture, API contracts, privacy, release rules) and Spec (approved subtitle UI/implementation plan, accepted small timestamp errors, two concurrent materials, editor busy-retry plan, validation feedback spec). Review old branches for duplication/defer instructions rather than merging blindly.
- [ ] Add a red/green release regression: running subtitles block switch, pending jobs survive maintenance, admission is atomic with the maintenance barrier. Keep existing schema 1 and no force cancellation/recharge.
- [ ] Run targeted regressions, JavaScript syntax, wheel/package inventory and complete Python suite from isolated candidate using the required Python interpreter. Preserve logs and exit codes privately.
- [ ] Commit design/inventory/verification and release runbook. Preserve prototype HTML/demo as archive-only artifacts, exclude historical deferred source picker/SAM scratch and old stashes from release code.
- [ ] Merge verified candidate into main without overwriting local notes/older untracked files. Verify resulting main identity and clean committed artifact. Retain running dev worktrees.
- [ ] Build and checksum Git bundle/source archive from the committed revision; inspect artifact for secrets/data. Record production enablement/configuration and task-drain/rollback commands. No production publish or service restart in preparation.

Verification commands use `~/miniconda3/envs/py3.12/bin/python3.12 -m pytest source/tests -o addopts= -q --tb=short` with `PYTHONPATH=source` from the integration worktree. Run `node --check` on changed JS; inspect built wheel for `editor-busy.js` and `preview-subtitles.js`. Release through `source/deploy/linux_ops.py` / the installed Linux manager, never legacy install commands. Keep provider keys out of logs and Git; runtime enabling remains a reviewed deployment step.
