# Hook 预览误改草稿：修复与验收

本次仅处理 Hook lane；基线为 `0138bd5626e609a2c60ccb9a82f7933816d3842d`，工作分支 `fix/hook-session-repair`。未访问生产、凭据、外部平台或修改共享 runbook。

## 原因与引入点

- `abd089fd` 的 `watchHookJob` 在完成时向 `current.draft.rows[].hook_options.preview_key` 写值并调用 `changed()`。由此把没有用户编辑的页面置脏，多素材恢复时第一条置脏也会阻止其余结果展示。同提交的 `paintHookTask` 按 pending 独立设置 disabled，覆盖只读编辑器权限。
- `5e8cefe7` 引入的 `_semantic` 将嵌套 preview_key 算入内容指纹，仅保存凭证就使 desired_generation 增长。
- `043b11d` 引入上传前 dirty 保护；此保护本身需要保留。更早同步预览写草稿的逻辑已见于仓库最早可追溯快照 `32c4360`，不能将全部行为归为近期新增。

## 最终行为

1. 前端完成结果只进入 hookPreviews，不改草稿、不置脏、不自动保存。真实编辑继续挡住上传；两个素材均可展示结果。
2. Hook 按钮通过现有 controls 同时计算编辑权限、busy 和任务 pending，完成回调/提交 finally 不再直接启用只读按钮。
3. 后端仅从内容比较中排除 hook_options.preview_key，空 receipt 容器与字段缺失等价。seq、request_id 和真实 Hook 设置仍参与原来的内容/身份约束。保存仍保留客户端传来的历史字段以兼容，但新确认不会直接信任其中的凭证。
4. 纳管 confirm 和 legacy 首次 start 在生成新执行 manifest 时，从本批次 hook-previews 目录选择本素材 completed report。校验 request_fingerprint、完整配置指纹、原片/索引 stat 身份、凭证格式，并调用原有 approved_preview 校验缓存内容摘要、输入、原片内容 hash、镜头索引 hash。多个素材共享一次确认内的 source hash 缓存，沿用 renderer 现有 source_cache 接口。
5. 凭证只进入新执行 manifest，草稿保持不变。旧 confirmed manifest 重试和纳管候选重复确认继续复用原 manifest。renderer 校验逻辑没有修改。缺少有效 report/缓存时清除新 manifest 的凭证并走既有重新分析流程。

## Red 证据

以下命令均使用 `/Users/matao/miniconda3/envs/py3.12/bin/python3.12`。

- UI 最初重跑发现旧 WIP fixture 的 `lambda route, job=job` 被 Playwright 的第二个 request 参数覆盖，导致 JSON 序列化错误和 30 秒等待。修正为 `lambda route, request, job=job` 后，以下命令在 1.4 秒内确定失败，业务断言是 `dirty is False`，实际为 True：
  ```sh
  cd /Users/matao/code/worktrees/exact-cut-fbbm-handoff/hook-session-repair
  /Users/matao/miniconda3/envs/py3.12/bin/python3.12 -m pytest -x -q -m ui source/tests/test_hook_preview_draft_ui.py::test_restored_previews_leave_draft_clean_and_allow_publish
  ```
- 在 source 目录运行 `python -m pytest -x -q tests/test_hook_preview_confirmation.py`，仅保存 preview_key 时断言 generation 保持 1，实际为 2。修复 semantic 后通过。
- 在 source 目录运行 `python -m pytest -x -q -m integration tests/test_hook_preview_snapshot.py`，真实 worker 完成预览后首次 legacy 确认，manifest 的 preview_key 为 None，预期为 report 中的 SHA256。加入两条确认路径绑定后通过。

曾从工作树根运行新的 Python 导入测试，出现 ModuleNotFoundError；已改从 source 运行。它与旧 UI 夹具超时均不计入业务 red。

## 最终 Green 命令与实际结果

执行目录：`/Users/matao/code/worktrees/exact-cut-fbbm-handoff/hook-session-repair/source`。

```sh
/Users/matao/miniconda3/envs/py3.12/bin/python3.12 -m pytest -o addopts="" -x -q -m "not ui and not integration" tests/test_hook_preview_confirmation.py tests/test_material_versions.py tests/test_workflow.py tests/test_phase2_hook_workflow.py tests/test_phase3_hook_sampling.py tests/test_managed_editor_review.py
# 68 passed, 74 deselected in 6.12s

/Users/matao/miniconda3/envs/py3.12/bin/python3.12 -m pytest -o addopts="" -x -q -m integration tests/test_hook_preview_snapshot.py tests/test_hook_preview_source_identity.py tests/test_managed_editor.py tests/test_phase2_hook_workflow.py tests/test_phase3_hook_sampling.py
# 39 passed, 11 deselected in 32.88s

/Users/matao/miniconda3/envs/py3.12/bin/python3.12 -m pytest -o addopts="" -x -q -m ui tests/test_hook_preview_draft_ui.py tests/test_editor_polling.py tests/test_source_frame_ui.py
# 14 passed in 4.13s
```

- unit：公开 save/detail 验证凭证变化不重做，真实 Hook 文案和 seq 修改仍增加 generation；以及现有生命周期、编辑保护、工作流回归。
- integration：真实本地 worker/FFmpeg 预览、两种确认路径和 renderer。覆盖有效预览复用、配置/输入/原片/镜头索引/摘要失效、异素材/异项目、缺失 report（即便客户端提供有效共享缓存指针）、回退分析、旧 manifest 重试、纳管重复确认。晚到真实 worker 结果前后比较权威文件完整字节、草稿内容和本地已生成视频 SHA256。
- UI：Playwright 运行真实 app.js；HTTP route 隔离任务/上传边界，不是实际 FBBM 上传验收。包含双素材恢复、revision/草稿不变、真实编辑拦截和只读按钮。
- `git diff --check` 通过。共 121 项定向检查通过；未运行全仓完整套件，未发布。

## 父级 review 重点与兼容边界

- app.js 与另一 lane 都可能相邻改动，合并时保留 api/refreshSelectedBatch/上传 CAS 的另一 lane 方案。本次只改变 Hook 回调、按钮权限和一处预览说明。
- `bind_confirmed_previews` 只由两处新确认调用；不能将它放到读取 detail、恢复预览或 renderer 动态读取最新 report 的路径，否则会破坏确认版本冻结。
- 旧已确认 manifest 不重新绑定；旧草稿仅有 preview_key 但没有本批次有效 report 时，新确认会重新分析。这是有意的信任边界，不做持久数据迁移，schema 仍为 1。
- 有匹配 report 的新确认会进行原片内容 hash 校验，带来确认阶段文件读取；同次确认已共用 hash 缓存。没有匹配 report 不读取视频内容。实际大文件确认耗时未作性能基准。
- source/index 在确认后的变化仍由 renderer 原有 approved_preview 检查阻止复用；没有降低或删掉输入/来源/摘要保护。
