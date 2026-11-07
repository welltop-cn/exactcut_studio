# 2026-11-07 三项线上缺陷修复：原片续传、纳管任务结束、发布修订冲突

状态：三项缺陷已按 TDD 修复并**已发布到生产**。候选 `4a5bc20aeb7f7e7e4793954c17b6fb5fcd146868`，线上 release `us-east-20261107-errors-4a5bc20`（前驱 `us-east-20261107-first-load-f40e59f`），镜像 `sha256:4f3e8454e8376053ffaaf43f305f5f0c8d461a6947f62dbce37f7f929997771e`。

## 1. 原片流传中断导致下载失败（EP25 类）

**线上证据**：11-06 有 11 次 `http.failed`，`/api/batches/dfdb472b…/originals/EP25/file`，错误 `ChunkedEncodingError / IncompleteRead(33717782 bytes read, 79363511 more expected)`；重读整个对象（113,081,293 字节）当时可完整取回，说明是上游流传瞬断。

**原因**：`webserver._send_original` 只捕获 `BrokenPipeError`/`ConnectionResetError`；`requests` 的流中断异常直接抛出，响应头已发出 200+Content-Length，客户端只能得到截断文件。

**修复**：流中断时用新签名的链接、`Range: bytes=<已发送>-` 续传同一个响应体，最多 3 次；续传失败或超出次数时抛出原异常（保持 `http.failed` 可观测）。`originals.open_original(start=…)` 要求续传必须返回 206，否则按不可用处理。

**回归**：
- `tests/test_originals_resume.py::test_interrupted_stream_resumes_from_the_bytes_already_sent`：先红（无 `resume` 参数）→ 绿。
- `…::test_resume_gives_up_after_bounded_attempts_and_surfaces_the_error`：上限与异常上抛。
- `…::test_open_original_range_requires_a_partial_response`：非 206 拒绝。
- 旧用例 `test_diagnostics::test_http_failed_original_context_and_signed_url_redaction`（断言 `ValueError` 仍然上抛、签名脱敏）保持通过，说明只对网络流异常续传，不吞其它错误。
- 自查发现的“`finally` 误关刚重连的流”先由加严的测试用例复现（`stream used after close()`），再修正。

## 2. 纳管项目结束任务时抛错，拖垮 worker tick

**线上证据**：11-04 09:00 `python.log` error `Runner tick failed; retrying without dropping queued work`，栈为 `finish_job → workflow.end_execution → workflow.update → material_versions.reject_legacy_write`，`ValueError: 此项目已纳管，必须使用素材版本操作`。连续 3 次即让 `/healthz` 降级；11-06 有 6 次整 5 分钟的 503。

**原因**：`end_execution` 对已纳管项目仍调用旧运行时/结果写入器，这些写入器按设计拒绝纳管项目；异常从调度 tick 冒泡。

**修复**：`end_execution` 对已纳管项目直接返回（纳管项目的执行状态由版本分片与 `material_workers` 负责），旧项目行为不变。

**回归**：
- `tests/test_managed_end_execution.py::test_end_execution_on_managed_batch_does_not_write_legacy_runtime`：修复前抛 `ValueError`（与线上同一条消息），修复后不抛、`runtime.json` 不变、不生成旧 `production/<rid>.json`、revision 不变。
- `…::test_end_execution_on_legacy_batch_still_updates_runtime`：旧项目仍写入 `executor_running=false`。

## 3. 发布反复 409「项目已在其他页面更新」

**线上证据**：近 72 小时 48 次 publish 409，涉及 producer01–07；`98e3905d` 在 10:38–10:44 同一账号连续 10 次。

**原因**：`api()` 会把 `current.revision` 带进纳管写请求；revision 落后时服务端 CAS 拒绝。前端被拒后既不同步版本，也不提示可恢复动作，于是每次重试都用同一个旧 revision，形成死循环（轮询在页面隐藏或未刷新时不会推进 revision）。

**修复**：识别「纳管 + 409 + 已在其他页面更新」时，后台触发一次项目详情同步（复用 `refreshSelectedBatch`），并把错误改成“页面已同步最新状态，请再操作一次”。不自动重放写请求——保存/确认类操作若自动重试会覆盖另一个页面的编辑。

**回归**：
- `tests/test_publish_conflict_resync.py`：Node VM 执行真实 `api()`；首次 409 必须触发一次同步、抛出可恢复提示；同步后再请求成功。修复前失败（不触发同步、提示误导）。

## 边界

- 未改变 CAS 语义、幂等令牌、35 dB / 帧数等画质门禁，也未放宽发布或交付守卫。
- 续传只覆盖上游流中断；签名失效、对象缺失仍按原错误上报。
- 本次未在生产复现写操作类场景（publish / save），修复效果需在发布后用真实只读与受控操作确认。
## 发布与线上核验

发布前只读 `check-switch`：`allowed=true`、阶段/待核实阻塞为空、活动任务为空。使用候选自身管理器发布，退出码 0、耗时 64.8 秒，切换记录 `status=ok`，维护状态已解除。

完整回归（同一固定提交）：

| 环境 | 结果 | 用时 |
| --- | --- | --- |
| 本机独立 clone，Python 3.12，清空 marker 过滤 | 2565 passed、10 skipped、10 warnings，退出码 0 | 775.65 秒 |
| Linux 发布管理器标准完整回归 | 2158 passed、417 skipped、10 warnings，退出码 0 | 718.88 秒 |

发布后实读：

- 健康：release 为新版本、`status=ok`、`maintenance=false`、`worker_alive=true`。
- 公网入口 200、侧栏 200（226 个项目）、项目详情 200。
- 页面引用的 8 个 JS 资源均 200，内容哈希与固定候选源码一致。
- 用报错项目 `dfdb472b…` 的 `EP25` 走 `/originals/EP25/file`：HTTP 200、返回 `video/mp4`、读取前 256 KiB 正常——即新的续传路径本身可用。
- 发布后 1 小时日志中没有新 error；仍存的记录都在发布前（含我未登录访问 `app.js` 的 401）。


## 未覆盖

- **未能人工制造上游断流**，因此“中断→Range 续传”只在单测与真实流读取路径上验证，生产未触发真实续传。
- publish 409 的自动同步只在 Node VM 单测验证；生产上未用写操作复现该场景（需真实发布到 FBBM，未授权）。
- 纳管任务结束的修复由单测覆盖，生产未再出现同类 tick 失败，但不能证明所有旧任务路径都已排除。
