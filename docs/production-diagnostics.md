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
