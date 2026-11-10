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
4. 纳管 confirm 和 legacy 首次 start 在生成新执行 manifest 时，从本批次 hook-previews 目录选择本素材 completed report。确认内只校验 request_fingerprint、完整配置指纹、原片/索引 stat 身份、凭证格式、缓存 JSON 摘要、输入排版身份与镜头索引 hash。缓存中的原片 hash 来自已发布 report 对应的 worker，本阶段不重新读取整片；实际原片内容 hash 继续由 renderer 原有 approved_preview 在复用前核对。
5. 凭证只进入新执行 manifest，草稿保持不变。旧 confirmed manifest 重试和纳管候选重复确认继续复用原 manifest；纳管已有 queued/processing 候选或当前版本 generation 已满足 desired_generation 时，直接跳过相应素材的预览绑定工作。renderer 原有输入/来源/摘要/索引条件均保留，并新增非对象缓存/decision 的拒绝检查。缺少有效 report/缓存时清除新 manifest 的凭证并走既有重新分析流程。

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
# 复审后：73 passed, 74 deselected in 5.66s（D2 实测；首轮为 68 passed）

/Users/matao/miniconda3/envs/py3.12/bin/python3.12 -m pytest -o addopts="" -x -q -m integration tests/test_hook_preview_snapshot.py tests/test_hook_preview_source_identity.py tests/test_managed_editor.py tests/test_phase2_hook_workflow.py tests/test_phase3_hook_sampling.py
# 复审后：61 passed, 11 deselected in 48.72s（D2 实测；首轮为 39 passed）

/Users/matao/miniconda3/envs/py3.12/bin/python3.12 -m pytest -o addopts="" -x -q -m ui tests/test_hook_preview_draft_ui.py tests/test_editor_polling.py tests/test_source_frame_ui.py
# 14 passed in 4.13s
```

- unit：公开 save/detail 验证凭证变化不重做，真实 Hook 文案和 seq 修改仍增加 generation；以及现有生命周期、编辑保护、工作流回归。
- integration：真实本地 worker/FFmpeg 预览、两种确认路径和 renderer。覆盖有效预览复用、配置/输入/原片/镜头索引/摘要失效、异素材/异项目、缺失 report（即便客户端提供有效共享缓存指针）、回退分析、旧 manifest 重试、纳管重复确认。晚到真实 worker 结果前后比较权威文件完整字节、草稿内容和本地已生成视频 SHA256。
- UI：Playwright 运行真实 app.js；HTTP route 隔离任务/上传边界，不是实际 FBBM 上传验收。包含双素材恢复、revision/草稿不变、真实编辑拦截和只读按钮。
- `git diff --check` 通过。首轮 121 项；复审后 D2 实测 unit 73 + integration 61 + UI 14 = 148 项目标回归通过，另跑全量 unit 层 1591 passed / 1 skipped。未运行全仓集成层全量套件，未发布。

## 父级 review 重点与兼容边界

- app.js 与另一 lane 都可能相邻改动，合并时保留 api/refreshSelectedBatch/上传 CAS 的另一 lane 方案。本次只改变 Hook 回调、按钮权限和一处预览说明。
- `bind_confirmed_previews` 只由两处新确认调用；不能将它放到读取 detail、恢复预览或 renderer 动态读取最新 report 的路径，否则会破坏确认版本冻结。
- 旧已确认 manifest 不重新绑定；旧草稿仅有 preview_key 但没有本批次有效 report 时，新确认会重新分析。这是有意的信任边界，不做持久数据迁移，schema 仍为 1。
- 确认阶段不再调用 source_fingerprints/file_sha256 读取原片完整内容。仍保留既有时间边界读取、report/cache JSON 与镜头索引检查，不把这项修改表述为确认完全无文件 I/O。
- source/index 在确认后的变化仍由 renderer 原有 approved_preview 检查阻止复用；没有降低或删掉输入/来源/摘要保护。

## 合入前复审：去除持锁整片 hash，兼容损坏派生数据

父级 review 指出的风险属实，前一版在确认锁内做完整原片 hash 的实现不应合入。已在首轮提交 `651a6a1` 后修正。

### 实际持锁链

- 纳管 HTTP：`webserver.do_POST` 对 material_submission 外包 `mutation_lock()` → `Handler.material_submission(action='start')` 内 `workflow.naming_lock()` → `material_versions.confirm` → `_confirm` → `bind_confirmed_previews`。
- legacy HTTP：`webserver.do_POST` 最后通用 `mutation_lock()` → `handle_post(action='start')` → `workflow.start_batch` 内 `naming_lock()` → `_start_batch` → 仅首次确认调用 `bind_confirmed_previews`。
- 原实现 binder → `renderer.source_fingerprints` → `util.file_sha256` → `Path.open('rb')`。即使本次确认共享 hash 缓存，首次大文件读取仍会占据全局 mutation 和 naming 锁。

### 确定性 red/green

在同一 source 目录执行：

```sh
/Users/matao/miniconda3/envs/py3.12/bin/python3.12 -m pytest -o addopts="" -q -m integration tests/test_hook_preview_snapshot.py -k "does_not_read_video or malformed" --maxfail=3
```

修复前实际 `3 failed, 20 deselected in 2.32s`：legacy、managed 的视频读取拦截均报 `confirm read video bytes while locks held: ['mutation.lock', 'naming.lock']`；第三条 JSON 数组 report 报 AttributeError。

测试使用合成短视频和真实 worker 生成 report，在公开 confirm/start 生命周期外复现 HTTP 已核实的锁上下文。拦截 Path.open 的原片读取，并用独立文件描述符的 `flock(LOCK_EX | LOCK_NB)` 核实两把锁确实被占用，不依赖 GB 文件耗时或计时阈值。首次确认之后继续拦截 report/cache 打开，验证已确认版本重复 start/confirm 不再进行预览读取。

修复后同一边界命令（`-x` 替代 `--maxfail=3`）为 `20 passed, 20 deselected in 12.39s`。随后加入确认后替换原片的真实 renderer 回归，最终完整目标 integration 为上述 61 项通过。

### 损坏数据边界

- report 为 list/null、result 为 list/string、completed_at 为不合法类型：跳过，不让可选派生数据阻断确认；还存在合法 report 时继续选择合法项。
- cache 为 list/null、identity 为 list、decision 为 list（即使按损坏文档重新生成合法 JSON 摘要）：不绑定，正常确认并保留重新分析路径。
- 旧已确认指针读取 list/null/number/string cache 或非对象 decision：approved_preview 返回 None。新增五条 unit 用例；renderer 原有 `sources_complete`、JSON 摘要、输入、实际原片 hash、镜头索引检查全部保留。
- 新增真实原片在确认后替换的回归：冻结 pointer 仍在 manifest 中，但 renderer 拒绝 preview_approved 并成功生成重新分析结果，证明轻量确认没有把陈旧原片 hash 视为最终授权。

### D2 接手后的实测复核

上一会话在 `gateway_concurrency_limit` 处中断，其报告中的测试计数未经本会话运行，不作为结论。以下为 D2 在本工作树实际执行的输出。

**1. 先证明 guard 是 red-capable（不是空断言）。** 临时把 binder 中的缓存 hash 来源改回 `renderer.source_fingerprints(project, timeline)`（即旧实现的整片 hash），不改测试：

```sh
/Users/matao/miniconda3/envs/py3.12/bin/python3.12 -m pytest -o addopts="" -q -m integration tests/test_hook_preview_snapshot.py -k "does_not_read_video"
# 2 failed, 40 deselected in 1.76s
# Failed: confirm read video bytes while locks held: ['mutation.lock', 'naming.lock']
#   两个参数项 legacy / managed 均失败
```

随后立即用备份还原 `hook_preview.py`（`git diff --stat` 恢复为 18 insertions / 8 deletions，`rg source_fingerprints exactcut/hook_preview.py` 只剩 `compute()` 内部第 10/32 行两处原有调用），重跑同一命令：`2 passed, 40 deselected in 1.48s`。因此该测试确实随"锁内读整片"的红而红、随修复而绿。

**2. 边界用例（锁 + 坏缓存 + 重复确认）：**

```sh
... -m pytest -o addopts="" -q -m integration tests/test_hook_preview_snapshot.py -k "does_not_read_video or malformed"
# 20 passed, 22 deselected in 12.85s
```

**3. 三层目标回归（D2 实测）：**

```sh
# unit
... -m pytest -o addopts="" -q -m "not ui and not integration" tests/test_hook_preview_confirmation.py tests/test_material_versions.py tests/test_workflow.py tests/test_phase2_hook_workflow.py tests/test_phase3_hook_sampling.py tests/test_managed_editor_review.py
# 73 passed, 74 deselected in 5.66s

# integration
... -m pytest -o addopts="" -q -m integration tests/test_hook_preview_snapshot.py tests/test_hook_preview_source_identity.py tests/test_managed_editor.py tests/test_phase2_hook_workflow.py tests/test_phase3_hook_sampling.py
# 61 passed, 11 deselected in 48.72s

# ui
... -m pytest -o addopts="" -q -m ui tests/test_hook_preview_draft_ui.py tests/test_editor_polling.py tests/test_source_frame_ui.py
# 14 passed in 2.82s

# 全量 unit 层（默认 addopts 对应层，确认签名/校验改动无其他调用方回归）
... -m pytest -o addopts="" -q -m "not ui and not integration"
# 1591 passed, 1 skipped, 1292 deselected in 153.56s
```

`bind_confirmed_previews` 仅由 `workflow.py:815`（legacy 首次确认）与 `material_versions.py:443`（纳管 confirm）调用；`approved_preview` 调用方为 binder 与 `renderer.py:315`，签名未变。

### 还原的校验边界（未削弱）

- 确认阶段仍校验：report 属本批次本素材、`request_fingerprint`（已排除 preview_key）、完整配置指纹、原片与镜头索引的 stat 身份、凭证格式、缓存 JSON 摘要自洽、缓存 `identity` 的输入/排版/镜头索引、`sources_complete`。
- 确认阶段不再解析原片内容：`sources` 取自该缓存文档自身记录的 hash（worker 在该 stat 身份下发布），因此这一步是"结构自洽 + stat 一致"，不是对当前文件内容的重新授权。
- 实际原片内容 hash 仍由 `renderer.render_request` → `approved_preview(project_dir, request, safe_timeline, source_fingerprints(...))` 在真正复用前计算并比对；`test_renderer_rechecks_source_content_after_confirmation` 用替换后的真实视频证明这一点。
- 确认阶段仍会调用 `local_timeline`，其中 `ffprobe` 只读容器/流头部以解析时长与越界，不读取整片字节；这是既有"输入校验"要求，不等同于全量 hash。
