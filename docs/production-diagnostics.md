# 诊断日志与问题定位

本页说明当前代码的查询入口。历史日志建设、上线时间及早期症状见[历史说明](archive/production-diagnostics-before-cleanup-2026-11-04.md)。

## 收集线索

先记下账号、发生时间及时区、项目 ID、素材编号、操作步骤和完整错误提示。页面有“诊断编号”时一并提供，它对应 HTTP 的 `X-Request-ID` 和服务器 trace。

项目、素材编号、任务、trace 和浏览器页面实例是不同维度。一次请求的 trace 不代表整个项目历史，重试还要看 attempt。应用日志使用 UTC，北京时间查询时注意时差。

## 查询

应用 JSONL 位于数据根的 `logs/`。在实际应用容器中使用已有诊断 CLI，不输出环境文件或完整凭据：

```sh
docker exec <应用容器> python -m exactcut.diagnostics --hours 2 --errors --limit 100
docker exec <应用容器> python -m exactcut.diagnostics --batch <项目ID> --hours 24 --limit 300
docker exec <应用容器> python -m exactcut.diagnostics --job <任务ID> --hours 24
docker exec <应用容器> python -m exactcut.diagnostics --trace <诊断编号> --hours 24
docker exec <应用容器> python -m exactcut.diagnostics --batch <项目ID> --request R001
docker exec <应用容器> python -m exactcut.diagnostics --actor <账号> --hours 2
```

还可按 `--client`、`--page` 或 `--event` 缩小范围。先按项目与时间取得完整上下文，再沿任务和 trace 追踪。

## 症状与证据

| 症状 | 重点事件或检查 |
| --- | --- |
| 登录失败、页面打不开 | `http.completed`、`auth.login_rejected`、`service.started`，代理状态与路径 |
| 点击后没有结果 | `browser.event`、`http.started/completed/rejected`、`job.state`；区分未到服务端、被拒绝和响应丢失 |
| 下载/制作一直排队 | `scheduler.snapshot`、`job.waiting`、控制状态、资源租约、磁盘预留与退避截止 |
| 正片缺失或下载慢 | `source.fetch_started/progress/fetch_failed/timeout`、共享原片锁、源配置 |
| Hook 或编码失败 | `render.plan/step/request_failed`、`ai.http/retry/responses.failed`，结合输入指纹 |
| 时间码与片长不符 | `render.segment_plan/segment_qc/qc`，对照原片索引及已确认清单 |
| 预览或剪切打码播放失败 | `media_error/waiting/stalled/playing`、播放器 source/session、HTTP Range 与代理耗时 |
| 交付失败或平台未就绪 | `oss.upload`、`feishu.*`、`fbbm.*`、`publication.state/failed`，核对既有远端记录 |
| 重启后任务异常 | `service.started`、`job.started/state`、`stage.result`、进程身份与接管证据 |

浏览器事件是辅助线索。HTTP 206、写入完成或没有错误事件，都不能独立证明浏览器已解码播放。队列 done 也不证明新成片已成为 current；查权威版本、QC、音频、哈希和回执。

## 保留与边界

`logging` 段通过集中配置设置。源码默认保留 14 天、单文件 20 MiB、应用日志总量 1 GiB，先到上限可能提前淘汰。清理由运行中的应用触发；任务审计 JSON 和代理日志另有策略，不受这个总量自动管理。

应用会对常规轮询采样，对浏览器事件按账号限额与去重。离线、限流或日志写入失败时证据可能不全，“没有日志”不等于“用户没有操作”。

不记录请求/响应正文、AI prompt、Cookie、认证头、完整命令行、签名 URL 或局部变量。分享日志前再次确认脱敏；page/client 标识用于诊断，不用于授权。

OOM/SIGKILL 需结合容器 `OOMKilled`、ExitCode 和宿主机日志，不能只按最后一条 started 推断根因。截图与实际媒体仍是视觉问题的必要依据。

代理观测模板在 [nginx-observability.conf](../source/deploy/nginx-observability.conf)。修改或 reload 线上代理属于生产变更，按既有流程授权并记录，不作为查日志的默认步骤。

## 锁与调度阶段计时（B0）

`web` 与 `batch_control` 两把锁记录等待和持有耗时，默认慢锁阈值为 **200ms**（`lock_timing.SLOW_MS`）。重入锁只统计最外层获取与释放，不改变锁范围、顺序或调度决策；新计时事件在当前线程释放所有计时锁后写入，观测异常不影响业务。

| 事件 | 字段与频率 |
| --- | --- |
| `lock.slow` | `lock` 为锁名；`owner` 为本次获取者的低基数标签，HTTP 使用方法与路由模板，32 位十六进制 ID 归一化为 `:id`，其他动态部分为 `:part`，不含查询串或用户名；worker 的 `owner`（含 `lock.summary` 的 `owners`）固定为 `worker.tick`，等待者的 `blocked_by` 仍使用 worker 实时阶段标签（如 `worker.reconcile`）。`kind` 为 `wait` 或 `hold`；`wait_ms`、`hold_ms` 为本次最外层获取的毫秒数；等待事件的 `blocked_by` 是开始等待时持有者的标签，随 worker 当前阶段更新，持有事件该字段为空。等待或持有达到阈值时触发，同一锁、owner、kind 每 30 秒最多一条。 |
| `lock.summary` | 每把锁每 60 秒最多一条，释放时顺带触发，无后台线程。`owners` 按获取时的 owner 汇总：`count` 为最外层获取次数，`wait_ms_total/max`、`hold_ms_total/max` 为该窗口累计值与最大值，`slow_count` 为等待或持有达到阈值的获取次数。窗口从锁初始化或上次汇总开始，空闲时不写日志。 |
| `scheduler.tick_slow` | tick 持锁达到 200ms 时触发，每 60 秒最多一条。`total_ms` 为整个 tick 耗时，`lock_hold_ms` 为 web 锁持有耗时，`phases_ms` 为阶段耗时映射：`recover_outbox`（锁外恢复）、`drain`（队列排空）、`lock_wait`（等待两把锁）、`poll_finish`（进程完成处理）、`recover_jobs`、`external`（外部进程统计及任务扫描）、`reconcile`、`durable_scan`（持久化排队任务扫描及二次排空）、`resource_usage`、`dispatch`。异常退出的 tick 也统计已执行阶段。 |
| `scheduler.tick_summary` | 每 60 秒最多一条，在 tick 结束时触发；`tick_count` 为窗口 tick 数，`phases_ms` 中每阶段的 `total_ms`、`max_ms` 为窗口累计与最大耗时。 |

`http.completed` 与 `http.failed` 增加 `lock_wait_ms`、`lock_hold_ms`、`lock_count`（web 锁）；获取过控制锁时还有 `batch_control_lock_wait_ms`、`batch_control_lock_hold_ms`、`batch_control_lock_count`。毫秒字段保留一位小数。原有 HTTP 采样规则不变：成功 GET 且耗时小于 2000ms 时按原规则节流，其他完成事件照常保留。

```sh
python -m exactcut.diagnostics --event lock.slow --hours 2
python -m exactcut.diagnostics --event lock.summary --hours 2
python -m exactcut.diagnostics --event scheduler.tick_slow --hours 2
python -m exactcut.diagnostics --event scheduler.tick_summary --hours 2
python -m exactcut.diagnostics --trace <响应头X-Request-ID> --hours 2
```

先从慢请求响应头的 `X-Request-ID`（等于 `http.completed` 的 `trace_id`）查询同一 trace 的 `lock.slow`，看等待耗时及 `blocked_by`。例如 `worker.reconcile` 表示等待开始时 worker 正在协调阶段，再按相近时间查 `scheduler.tick_slow` 的 `phases_ms.reconcile`。worker 本身可能没有请求 trace，周期汇总也不代表单次请求；被节流的事件缺失不能证明没有锁竞争。
