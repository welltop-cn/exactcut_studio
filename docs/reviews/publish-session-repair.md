# 上传 revision 与页面状态同步修复：验收及独立安全差异审查

## 结论

本轮完成上传事务与页面同步修复，供父级最终 review/整合。审查建议 **CONDITIONAL：本范围未发现未解决的高危问题，合并由父级确认与另一 lane 的组合结果**。未发布、未访问生产/凭据/外部平台，未启动交付 worker。只在指定独立工作树修改、测试、提交；未写共享 runbook，未派子代理。

| 严重级别 | 未解决发现数 |
| --- | ---: |
| CRITICAL | 0 |
| HIGH | 0 |
| MEDIUM | 0 |
| LOW | 0 |

改动本身按 HIGH 风险审查，因为 delivery revision 校验改变且通向外部交付 outbox。768 个跟踪文件，采用 SURGICAL 策略，手工审查本次全部产品 diff 与 1-hop 入口/调用链，未做仓库全量安全审计或覆盖率百分比声明。

## 工作位置与范围

- 工作树：`/Users/matao/code/worktrees/exact-cut-fbbm-handoff/publish-session-repair`
- 分支：`fix/publish-session-repair`
- 接续 HEAD：`ef9d8a2ef11d912d857c4d12190aedd263cbc8eb`，WIP 已在基线，无 cherry-pick。
- 原修复基线：`868f570c9e759478b6305cfd4e17594a7f905e54`。
- 本轮产品文件：`source/exactcut/material_versions.py`、`source/exactcut/web/app.js`。
- 验证文件：`test_delivery_revision.py`、`test_publish_conflict_resync.py`、`test_publish_session_ui.py`、`test_managed_editor_review.py`。
- 原有孤立 handler 的 publish 导航断言迁入完整 click→operation→api→refresh 流程；其他 mutation 的原断言保留。
- 不改 Hook dirty 判定、预览凭证、旧保留成片与已编辑草稿的既有交付语义。

## 引入点与实际根因证据

1. `git blame -L 248,266 ef9d8a2 -- source/exactcut/material_versions.py`：全项目 revision CAS 与幂等账本来自 `5e8cefe7`（2026-10-11）。`material_workers.py:104/353` 等后台交付/版本提交继续推进 revision。因此只有其他素材交付或后台进度发生变化，也会使仍有效成片的旧审批被拒绝。
2. `git log -S '页面已同步最新状态' --oneline -- source/exactcut/web/app.js`：`faf29bc` 引入冲突后未等待应用完成即声称同步的提示；`d28c338` 后续使用 force，并非最早引入点。
3. 接续 HEAD 的 publish handler 直接 `current=result`，未保留等待期间的本地修改；不同素材点击没有提交回执队列。
4. 接续 HEAD 的 api 幂等 key 含 revision。未知回执后 revision 推进，再点击同一成片会生成不同 token 和 payload，无法查询同一事务。

历史记录没有客户端 revision，不将线上全部历史 409 归因于某一个具体 race。

## 行为变化

### 服务端

`material_versions.py:248` 的 `_mutation` 仅在内部 action 为 `delivery` 时接受 `1 <= revision <= state.revision` 的真正整数；缺失、bool、float、字符串、零/负数、未来值拒绝。save/confirm/control/restore/verify-delivery 等保持原项目 revision 比较，mutation digest 与不可变 receipt 保持。

`material_versions.py:489` 的 `_queue_delivery` 保留 feature/config、项目/素材 gate 和锁外文件 SHA 校验。锁内依次检查项目暂停/归档/删除，素材存在/未删除、current_version、record.output_sha256、version.asset_id/version_id、master 路径与文件身份，blocking delivery、已就绪重复及文件名绑定。创建 delivery 与 outbox 使用同一锁内 `version`，不再沿用锁外 snapshot。

文件身份包含 dev、ino、size、mtime_ns、ctime_ns：hash 前取身份，锁内比对；拒绝 hash 后原子替换，也检测普通原地写入。master symlink 拒绝。遵守 gate 的编辑器无法在提交窗口改同素材；后台 frozen master 不覆盖。任意绕过应用锁的文件写入仍可能发生在锁内检查之后，现有 `material_workers.materialize` 会复制到独立临时文件、核验 event SHA 后才准备作业，发生变化会停止，不能把这层 stat 比对称为对任意文件系统攻击的完全证明。

### 前端

- `app.js:74`：共享 api 保留原写请求追踪；新增完整请求保存只针对 managed `/publish`，其他 mutation、普通项目、GET 沿用原 token key/超时/错误处理。
- `app.js:559`：只有 GET 成功且目标项目、导航序号、revision/updated_at、busy/其他写请求条件允许应用时才报告同步；失败/未应用给真实提示，不自动 POST。
- `app.js:613`：每项目一条提交 Promise 链；等前一条提交回执，不等文件上传或 FBBM 处理。点击立即冻结 id/navigation/request_id/version_id/SHA/managed，发送前再检查导航、dirty、成片与交付/控制状态。失败后队列可继续，但不会重试失败动作。
- 上传成功回执通过既有受控详情合并，保留本地 dirty 草稿。服务端草稿相对保存基线有外部变化时不推进本地编辑 CAS；同草稿的后台变化才推进 revision。

## 未知回执的保留/清除边界

保存位置是本标签页 `sessionStorage`，managed publish key 由路径和不含 revision 的成片 payload 身份构成，value 是完整 payload（包含原 revision、submission_token）。

| 情况 | 处理 |
| --- | --- |
| 用户首次点击同一成片 | 保存完整请求，然后发送一次 POST |
| fetch TypeError、断连、AbortError/超时 | 保留原请求；没有自动 POST；用户再次点击且当前目标仍允许时重放原 payload/token |
| 已解析 5xx | 保留原请求；没有自动 POST；现有错误文本保留，并不把 5xx 当确定未接收 |
| 成功响应且 JSON 解析成功 | 清除对应 key；成功详情受导航/dirty/单调性护栏保护 |
| 普通已解析 4xx（含 400/409/429） | 清除对应 key；409 项目冲突可 GET 同步，但不会 POST 重试 |
| 401 | 原逻辑先跳登录并抛错，保留 key；不声称该分支清除 |
| 响应 JSON 读取/解析失败 | 保守保留 key（包括 malformed 4xx）；已接收状态可能未知 |
| 同标签页重新加载/同源导航 | sessionStorage 保留，完整请求仍用于用户显式重放；Chromium 实测 |
| 新开的独立标签页 | sessionStorage 独立，无客户端跨 tab 串行；Chromium 实测。带 opener 的 window.open 可能按浏览器语义复制初始 sessionStorage，此时复制 token 仍受服务端幂等保护，未实测 opener 分支 |
| 用户点击另一素材 | 独立 key/token，另一素材成功只删除自己的 key |
| 当前成片版本变化，即使 SHA 相同 | 已排队旧意图拒绝；新点击按新 version_id 建立独立 key，不复用旧审批 |
| 旧未知回执成片不再当前 | 旧记录保留，但不会重定向到新版本，也不自动查询/发送 |
| 详情已显示 pending/uploading/ready/unverified | 前端拒绝新提交；服务端仍有 gate、blocking/ready 与幂等账本保护 |
| 关闭标签页/浏览器清除 sessionStorage | 可能丢失客户端记录；服务端幂等/交付状态仍保护重复外部交付 |

旧版本已有的 `exactcut.submit.*` 条目不迁移到新增 `exactcut.delivery.*` namespace；历史 accepted delivery 仍由服务端 blocking/ready 与幂等账本保护。没有未经用户点击动作自动重新 POST。

## 实际 red → green

均使用 `/Users/matao/miniconda3/envs/py3.12/bin/python3.12`，从本工作树 `source/` 执行。

| 反馈环 | 实际 red | 最小修复后的 green |
| --- | --- | --- |
| 交接中的假同步 | 交接已记录 `must not claim synced before the GET finishes` 失败 | 本轮真实函数回归继续通过 |
| `pytest tests/test_delivery_revision.py -q` | 旧成片身份仍有效，save 推进 revision 后 queue_delivery 因全项目 CAS 拒绝，1 failed | delivery 限定 revision 校验后 1 passed |
| `pytest tests/test_publish_conflict_resync.py -q` | 两次点击实际产生 2 个 POST，期望 1；1 failed、1 passed | 回执队列后 2 passed |
| `pytest tests/test_delivery_revision.py -q`（新增 hash 后替换） | `DID NOT RAISE ValueError`，1 failed、1 passed | 锁内文件身份复核后与前端合计 15 passed |
| `pytest tests/test_publish_conflict_resync.py -k unknown -q` | retry 从 revision 7/token0 变为 revision 8/token1，1 failed | 保留同成片完整请求后通过 |

新增辅助测试过程也捕获并修正了 harness 问题：生成 token 的 stub 不应按 storage.size 重用值；init-script 的 api 需显式导出供 Playwright 读取。没有把测试装置错误计为产品 red。

## 最终验证与层次

最终主命令：

```sh
/Users/matao/miniconda3/envs/py3.12/bin/python3.12 -m pytest -o addopts="--strict-markers" -m '' -q tests/test_delivery_revision.py tests/test_publish_conflict_resync.py tests/test_publish_session_ui.py tests/test_project_switch.py tests/test_managed_editor_review.py tests/test_material_versions.py tests/test_managed_completion.py tests/test_workflow.py tests/test_webserver.py
```

**216 passed，3 warnings，14.96 秒**。其中 unit 96、integration 116、UI 4（此前 211 项按 marker collect-only 得到 92/116/3，新增 4 unit 与 1 UI 后为本次数量）。

- unit：Node VM 执行真实 click/operation/api/refresh/queue 函数，网络和 DOM 渲染作为边界替代。覆盖 GET 失败、A→B→A 导航、其他在途写请求、外部草稿、较新状态、等待期间 dirty、新版本/暂停/unverified/功能关闭、同素材重复、未知回执、4xx/5xx、不同素材。
- integration：公开 queue_delivery、真实文件、flock/持久状态，故障注入置于 hash/文件边界；真实后台候选 stage_enter/stage_finish、另素材提交与 HTTP 权限/回执。并发同素材两线程竞争只能一条 delivery/outbox。HTTP 使用临时端口、独立账号和合成数据，服务 shutdown/join/server_close 后断言线程退出。成功 enqueue 仅替代 require_publish 以允许隔离持久接收，不启动 worker；此前真实 require_publish 禁止分支也有断言。
- UI：Chromium 真实 DOM click/input 与原函数，网络和渲染副作用替代；验证等待回执时串行与输入保留。新增原生 sessionStorage 的同标签页 reload、新独立 tab 隔离测试；synthetic origin 的请求全部本地 route.fulfill，无外部访问。
- 额外共享 api/guard 单元检查：`test_web_base_path.py test_mosaic_ui.py test_publish_ready.py test_media_release_guard.py test_editor_release_guard.py -m unit`，**31 passed、165 deselected，3.79 秒**。
- 额外共享 api UI：`test_mosaic_ui.py -m '' -k 'real_api_scan_accepted_body_failure or resource_busy_open or close_cancels_busy_open_retry'`，**4 passed、157 deselected，1.24 秒**。
- `node --check source/exactcut/web/app.js`、`git diff --check` 通过。
- 3 warnings 来自原有 multiprocessing fork 在多线程进程内的弃用提示；没有失败。
- 扩大到整份上述 mosaic/UI 批次的运行在 60 秒命令限时被中止，没有完整 verdict，不能算通过；随后核对没有该 pytest/Playwright driver 遗留进程。未运行仓库完整 release suite，未发布。

## Blast radius 与信任边界

| 函数/入口 | 产品调用点 | 审查结论 |
| --- | ---: | --- |
| `_mutation` | 6 | save、restore、control、confirm、delivery、verification。只有内部 delivery action 放宽；其余原 CAS 不变 |
| `queue_delivery` | 1 个 HTTP 入口 | `webserver.py:1712` 保留 authorized、X-Exactcut-Request、can_access、maintenance、worker、require_publish |
| `_queue_delivery` | 1 个 gate 包装入口 | 全程原项目/素材 gate，事务内原 flock；同素材竞争可 429 或 blocking 拒绝 |
| `api` | 37 个静态调用点（不含定义） | 高横切影响；完整 payload 保留分支仅 managed publish，其他 API 经超时/路径/错误回归验证 |
| `refreshSelectedBatch` | 3 | poll、冲突 GET、安全合并成功回执。无新的外部写调用 |
| `submitPublication` | 1 | assets click。每项目 Map，仅回执串行，导航/dirty/身份再验证 |

计数来自 `rg`/源码静态扫描，非动态覆盖率或跨仓库调用证明。

## 具体对抗审查

| 发起者/攻击序列 | 结果/证据 |
| --- | --- |
| 无登录/无来源标识/其他项目用户 POST publish | HTTP 401/403/404，未创建 delivery |
| 只读实例或发布环境禁止仍 POST | HTTP 503/400，未创建 delivery |
| 持有旧 revision 但成片未变 | 允许 delivery；旧 revision save 仍拒绝 |
| 提交缺失/非法/未来 revision | 拒绝，未创建 delivery |
| 两个合法用户/标签页同时审批同素材 | gate/事务 blocking 只让一次接收，最终一条 outbox |
| lost receipt 后重复原 payload/token | 原 immutable mutation receipt，现态 projection，无新 outbox |
| 同 token 改 revision/payload | digest 不符拒绝 |
| unverified/ready 或暂停/归档/删除后持旧审批提交 | 拒绝；已知原 receipt replay 不新建事务 |
| hash 完成后文件替换或 current_version 切换 | 锁内身份检查拒绝，不生成 outbox |
| hash 后仅后台版本元数据改变 | outbox 使用锁内版本（generation=42 的故障注入用例） |
| 已绑定同文件名后尝试不同 SHA | 文件名冲突拒绝 |
| 用户等待回执时编辑/切换项目/换新版本 | 保留草稿/导航，旧意图不转向新目标；不自动重试 |

## 父级 review 关注点与限制

1. 与另一 lane 的 dirty 标记和预览凭证变更做最终整合；此处依赖既有 dirty 信号，不修改其判定。
2. 共享 api 的新保存分支严格限 managed publish；其他 37 调用点共用 pendingWrites/错误处理，已运行针对性回归，但没有宣称整个 UI 全量通过。
3. stat 身份复核与 frozen master/worker SHA 层共同防 TOCTOU；非协作文件系统写入在事务之后仍需由 worker hash fail-closed，不应移除后者。
4. 客户端按 tab 串行，跨 tab/进程的唯一性依赖服务端 gate+锁内 blocking+幂等；线程并发及已有跨进程 CAS 回归通过，不把线程测试冒充新的跨进程 delivery 测试。
5. sessionStorage 不是共享事务库，opener 初始复制及旧 namespace 条目迁移没有新增实现。任何真实外部接收结果仍以服务端 delivery/unverified 为权威；没有外部写自动重试。
6. 无生产验收、无真实 OSS/飞书/FBBM 写入。尚需父级 review 和合并后的适当回归；本轮提交后停止写入。
