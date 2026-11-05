> 历史归档（2026-11-04）。正文保留当时的方案或执行状态，不代表当前版本；日常操作见[文档入口](../../README.md)。

以下是只读 review 的结论。范围为提交 `76c8759`（预览/制作资源分离）与未提交的 `diagnostics.py`、`web/diagnostics.js`、`webserver.py`、`hook_preview.py`、`test_diagnostics.py`。我核对了 `source/AGENTS.md` 及相关实现（`storage.py`、`concurrency.py`、`batch_control.reconcile`、`workflow.detail`、`error_guidance.guidance`、`hook_emphasis`、`ai_rate` 等）。

## 结论

- **未发现阻塞性缺陷**。资源分离、磁盘预约恢复（`storage.rebuild_reservations(..., inline_ids=INLINE_RENDERS)`）、`X-Request-ID`/`trace_id` 关联、客户端预算与服务端字段白名单在代码层面都能自洽，且与新增测试一致。
- 需要发布前确认一项运维前提（非代码缺陷）。
- 其余为可选/健壮性问题，按严重度列出。

## 发布前提（非代码缺陷，但决定“独立并发 3”是否生效）

- `source/config/production.json:20` 已把模板值改为 `hook_previews: 3`，但代码读取的是运行配置（`/srv/exactcut/config/runtime.json`）。`docs/archive/verification/hook-preview-capacity-2026-10-12.md` 自己也写明“当前显式值仍为 1，仅改模板不覆盖运行实例”。若不同步 runtime 配置，预览上限仍是 1，本次改动只解除 render/总闸耦合。建议在发布清单中列为显式步骤，并核对启动日志 `Effective concurrency settings`。

## 可选问题（按严重度）

### 1. 中：服务端 `browser.event` 限流键包含客户端可控标识，可被绕过
- 位置：`source/exactcut/webserver.py:1668-1672`（键含 `client_request_id`、`route`、`status` 等），配合 `source/exactcut/diagnostics.py:196-203`。
- 触发：已登录客户端对 `/api/client-events` 每次使用不同的 `client_request_id`（或变更 `route/status/request_id`）发送事件。
- 影响：`interval=10` 节流对 `request_started/request_completed/network_error` 实际失效；每个请求都落一行；`_throttled` 超过 4096 会整体 `clear()`，连带把真实 `page_error` 等需要去重的键也清掉，造成日志频繁轮转、旧诊断被淘汰。
- 最小修复：限流键只用不可被单一请求轻易改变、且真正表达“同一问题”的字段（如 `('browser', actor, event, batch_id, action, error_type)`），把 `client_request_id/route/status` 从键中移除；或增加服务端按用户/分钟的硬上限。

### 2. 中：`startNow`（确认开始制作）不再被上报
- 位置：`source/exactcut/web/diagnostics.js:42-43` 的 `buttons` 集合；旧实现为 `['startNow','newBatch','saveDraft','uploadButton','confirmPublish']`。
- 触发：用户在确认弹窗点击 `startNow`（`source/exactcut/web/app.js:296`）。该按钮 `id` 不在新集合，且没有 `data-hookPreview/publish/sendFeedback/batch/batchAction/tab` 任一属性，走不到 dataset 分支。
- 影响：最关键的业务动作“开始制作”在浏览器诊断中丢失（相对旧代码是回归）；`saveDraft/uploadButton/confirmPublish` 分别被新的 `saveButton`/`data-publish`/`confirmButton` 覆盖，但 `startNow` 没有替代。
- 最小修复：把 `'startNow'` 加回集合（或改成只监听带 `data-action` 的统一约定）。

### 3. 低：`hook_preview.deferred` 的 `phase='compute'` 分支不可达
- 位置：`source/exactcut/webserver.py:1622-1631`。`compute` 抛出的 `ResourceDeferred` 已被 1622-1624 的内层 `except` 吞掉并转成 `deferred`，不会重新抛出；外层 `except ResourceDeferred` 只可能由 `admit_hook_preview` 触发，此时 `admission` 恒为 `None`。
- 触发：`compute` 阶段（例如 `ai_rate.acquire` 返回预算不足）触发 429。
- 影响：`phase='compute'` 永不出现；该路径实际记录为 `hook_preview.result_rejected`（`status=429`），预期中的 `hook_preview.deferred(phase=compute)` 缺失，排障时无法区分“准入拒绝 vs compute 降级”的语义字段。
- 最小修复：在内层 `except` 里就记录 compute 阶段的 deferred，或让外层 `phase` 依据 `deferred` 而非 `admission` 判定。

### 4. 低：诊断参数在 `diag.event` 前求值，字典下标缺失会把异常带入业务
- 位置：`source/exactcut/hook_preview.py:52-53`（`doc['sources_complete']`、`coverage['sampling_capped']`、`emphasis['source']`）。
- 触发：后续若 `approved_emphasis` 某个分支或 `sample_timeline_frames` 返回值缺键。
- 影响：`diag.event` 自身吞异常，但实参求值发生在调用之前，`KeyError` 会直接冒泡进 `compute`，把本应成功的预览变成 400，违反“诊断失败不影响业务”。
- 最小修复：用 `.get()`（如 `emphasis.get('source')`、`coverage.get('sampling_capped')`）或先取默认值。

### 5. 低：`respond` 直接索引 `error_info['detail']`
- 位置：`source/exactcut/webserver.py:1080-1081`。
- 触发：任何 `respond(code, {'error': ..., 'error_info': {...无 detail...}})` 调用。当前唯一显式调用点（`1826`）用 `guidance()` 产物，含 `detail`，所以暂不触发。
- 影响：一旦调用方传入不含 `detail` 的 `error_info`，会在发送响应前 `KeyError`，用户看到的是 400/500 而非真实错误，属于“诊断代码影响业务”。
- 最小修复：`'detail': (body['error_info'].get('detail') or '') + '\n诊断编号：' + self.trace_id`，并确认 `body['error_info']` 为 dict。

### 6. 低：`release_hook_preview` 异常会跳过响应并遗留预约
- 位置：`source/exactcut/webserver.py:793-797`，调用点 `1636-1637` 的 `finally`。
- 触发：`storage.release`（`reservation_lock` 的文件 IO）抛异常。
- 影响：`finally` 中异常传播，`if response: self.respond(*response)` 不再执行；同时账本中的 inline 预约要等下一次 `worker_tick` 的 `rebuild_reservations` 才被回收。
- 最小修复：`storage.release` 包一层 try/except 并记录诊断，保持 `INLINE_RENDERS` 已 discard 的行为，保证清理尽力而为、不改变业务响应。

## 测试是否确实覆盖

- 正面：`test_hook_preview_capacity.py` 确实用真实 HTTP 路由 + FakeProcessTable 验证了“3 个并发进入、第 4 个 429、释放后可再来、后台 render 计数不被预览污染”；`test_step3_resources.py` 验证了 `rebuild_reservations` 保留 live inline 预约、以及预览不再阻塞后台暂停进程 SIGCONT；`test_diagnostics.py` 验证了 `X-Request-ID`/`trace_id`/`error_info.detail` 与 client id 落库、字段白名单拒绝 NaN/字符串 status、`--actor/--trace` 查询。
- 不足可证实项：
  1. fetch 包装测试（`test_diagnostics.py:305-330`）把 `originalFetch` 完全 mock 掉，只检查新增头与 `req.bodyUsed === false`；`bodyUsed` 在该 mock 下是恒真的弱断言，**signal 传递、body 原样透传、`Request` 重建后的 method 语义都没有被验证**。建议补一个断言 `args[1].signal === signal`、`args[1].body === body`（或 `calls[0][1].body`）以及带 `Request` 的 method/body 保持。
  2. 磁盘预约的“端到端 HTTP 回收”只在 `INLINE_RENDERS` 层面断言（`test_hook_preview_capacity.py:53,97-99`），没有在请求成功/失败后直接断言 `reservations.json` 中 inline 记录已删除；`test_step3_resources.py` 的账本断言是直接调用 `release_hook_preview`，未覆盖“handler 异常路径”的 finally 释放。
  3. `test_round1_integration.py:24` 把 `web.INLINE_RENDERS` 替换成 `{}`（dict）而非 `set()`，一旦该用例触及 `admit_hook_preview` 的 `.add` 会以 AttributeError 失败/掩盖问题；当前未触发，但属潜在测试缺陷。
  4. `hook_preview.deferred` 的 compute 分支（问题 3）没有测试覆盖。

## 其他确认无误的要点

- 磁盘预约回收：`storage.rebuild_reservations` 通过 `inline_ids=INLINE_RENDERS` 保留在途预约，且 `release_hook_preview` 的 `discard`+`storage.release` 与 `worker_tick` 的 `rebuild` 都在 `LOCK` 下，互斥成立；进程崩溃后 `startup` 的 `inline_ids=()`（空）会丢弃陈旧 inline 预约，不会永久泄漏。
- fetch 的 header 语义：`new Headers(options.headers === undefined ? input.headers : options.headers)` 再 `set` 新头、`args=[input,{...options,headers}]`，对 string/URL/Request 三种输入都保持 method/body/signal/其余 init 字段；`report` 走 `originalFetch` 不会递归。
- 脱敏与白名单：`browser_fields` 按名白名单 + 数字有界有限 + 字符串正则，`safe()` 再做敏感键/文本脱敏；客户端不发送 URL/body/表单值（JS 测试也有断言）。
- `trace_id` 关联：错误响应体与 `error_info.detail` 都带 `self.trace_id`；`parse_request` 绑定 `X-Client-Request-ID` 后 `http.rejected` 落 `client_request_id` 可查询；后台阶段通过 `save(job)` 保留 job `trace_id`，可用 `--batch/--request` 关联。

再次强调：**没有发现阻塞性缺陷**；上面 1/2 两项建议在发布前处理，3–6 建议随下一次改动一并加固。
