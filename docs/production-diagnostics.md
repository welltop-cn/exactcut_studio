# 生产问题定位日志

需求依据：[试用反馈文档](https://welltop.feishu.cn/docx/IpVodvkPuol1yJx9TedcxVxKnph)，读取 revision 4（2026-10-10）。这些是历史症状，新增日志不能补出历史根因，也不代表原问题全部修复。

## 保存与读取

应用 JSONL：宿主 `/srv/exactcut/data/logs/events-YYYYMMDD-*.jsonl`，容器 `/data/logs/`。Web、阶段任务、预下载进程共同写入，进程间锁保护整条写入与轮转；目录 0700，文件 0600。UTC 日期分文件，单文件 20 MiB，默认最多保留 14 天，应用日志总量 1 GiB，先达到容量上限时会提前淘汰最旧文件。清理由写入触发，空闲 worker 每分钟记录一次状态；服务完全停止时不执行清理。

集中配置 `config/runtime.json` 的 `logging` 段覆盖 `config/default.json`：`enabled`、`retention_days`、`max_file_bytes`、`max_total_bytes`。修改后重启生效。日志策略不参与成片缓存指纹。原有任务日志/审计 JSON 保留，**不受新日志的容量上限管理**。

反向代理补充 JSON 日志：`nginx-access.log`，记录不带 query 的路径、HTTP 状态、总耗时、upstream 耗时/状态、应用 trace ID。单独 logrotate：每日/20 MiB 轮转，最多 14 份、14 天，小时检查；高流量时实际保留时间缩短。应用日志和代理日志是两套限额。

查询无需新增依赖，在服务器运行（容器名先用 docker compose/ps 查实际值）：

```sh
docker ps --filter label=com.docker.compose.project=exactcut-production --format '{{.Names}}'
docker exec <容器名> python -m exactcut.diagnostics --hours 2 --errors --limit 100
docker exec <容器名> python -m exactcut.diagnostics --batch <项目ID> --hours 24 --limit 300
docker exec <容器名> python -m exactcut.diagnostics --job <任务ID> --hours 24
docker exec <容器名> python -m exactcut.diagnostics --trace <X-Request-ID> --hours 24
docker exec <容器名> python -m exactcut.diagnostics --batch <项目ID> --request R001
```

每行包含时间、版本、环境、PID、事件类型；按适用范围携带 actor、batch_id、request_id（素材编号）、job_id、attempt、trace_id。HTTP 返回 `X-Request-ID`；提交产生的任务和后续阶段保留同一个 trace_id。重试看 attempt，跨 HTTP 操作看 batch_id，不要把一个 trace 当成项目的全部历史。

## 反馈症状与证据

| 反馈症状 | 先查事件 | 能区分什么 |
|---|---|---|
| 登录不上、页面打不开 | proxy.request、http.completed、auth.login_rejected、service.started | 请求是否到 nginx、是否到应用、401/403/404/502/503、路径迁移、worker 是否健康 |
| 点击提交无反应 | browser.event、http.started/completed/rejected、job.state | 页面脚本行号、未捕获异常、网络异常、提交是否被拒绝、是否创建任务 |
| 下载一直排队、失败项目占位 | scheduler.snapshot、job.waiting、batch.control_changed、process.signalled | 并发池满、单项目上限、暂停/归档、依赖活跃、证据暂不可读、磁盘预留不足、重试时间未到 |
| 无正片、下载慢/卡住 | source.fetch_started/progress/fetch_failed/timeout、source.lock_wait、source.unavailable | 剧和集、来源命令失败及脱敏错误尾部、字节数/总量、无进展秒数、共享锁等待、缺少下载配置 |
| 文案稍长制作失败、Hook 排版异常 | render.plan、render.step、ai.responses.failed、ai.http、ai.retry、render.request_failed | 文案字符数、渲染配置指纹、失败阶段、AI HTTP 状态、重试、异常文件/行/函数；正文不写日志 |
| 填 20 秒只剪 2 秒、时间码不准 | render.segment_plan、render.segment_qc、render.qc | 解析后集号/start_ms/end_ms、预期与实测时长、容差、在哪一段被拒绝；与项目 confirmed.json 对照即可区分输入解析和编码阶段 |
| 预览慢 | preview.generate、preview.ready、preview.stream、proxy.request | 预览生成耗时、轻量文件/正式成片大小、Range 范围、已发送字节和耗时、客户端断开、代理传输耗时 |
| 素材重名、失败后无法编辑 | http.validation_failed/rejected、batch.control_requested/changed | 拒绝原因、异常位置、项目状态；具体操作见路由，不把状态拒绝猜成网络错误 |
| 入库慢、上传失败、FBBM 不就绪 | oss.upload、feishu.*、fbbm.http/retry/state、publication.state/failed、stage.result | 哪个外部服务耗时、HTTP/业务错误码、平台原记录与素材 ID、等待时间、是可重试还是 unverified 待核实 |
| 重启后任务异常 | service.started、job.state、job.started、stage.result、scheduler.snapshot | 新版本/PID、是否接管、从哪个阶段重排、执行次数、子进程退出码和资源名额 |

## 边界与安全

- 正常健康探测不逐条写应用日志；常规 GET 轮询每路由/用户 30 秒采样；排队原因每任务/原因 60 秒采样。HTTP 错误、修改请求、慢请求完整记录。浏览器单页面最多 20 次报告，服务器同用户/事件 10 秒限频；刷新页面重置客户端额度。
- 浏览器报告只含枚举、数字和受限标识，不含错误正文、页面内容或表单；它是不可信客户端证据，必须对照服务器事件。浏览器完全离线时无法上传报告；不能用“没日志”直接认定没有点击。
- 不记录请求/响应正文、AI prompt、Cookie、认证 header、完整命令行或带签名的 URL；异常记录类型、脱敏消息与栈位置，不包含局部变量和源码行。下载错误只保留有界尾部。
- 日志写入失败或锁等待超时不会让制作失败；stderr 每分钟至多报告一次 diagnostic_write_failed。监控此信息：日志系统故障时不能保证证据齐全。追加已 flush，但不逐行 fsync；机器突然断电可能丢失最后部分日志。
- OOM/SIGKILL 无法由被杀进程自行记日志。结合 Docker 状态、`docker inspect` 的 OOMKilled/ExitCode，以及主机 kernel journal；最后一条 started 没有 completed 只是定位线索，不是根因结论。
- 画面审美、违规镜头判断、希望新增字体/ending 等产品需求不能靠日志证明“正确”；日志用于确认输入、执行版本和失败环节，视觉结论仍需看素材。

## 部署代理日志

将 `source/deploy/nginx-observability.conf` 安装到 nginx http context；只在 Exact Cut 的 server 中添加 `access_log /srv/exactcut/data/logs/nginx-access.log exactcut_diagnostic;`。备份配置，先 nginx -t 再 reload。logrotate 模板安装到部署根 config，使用独立状态文件每小时调用，避免与系统 logrotate 重复管理。保留原 nginx error log 用于 upstream 连接失败等系统诊断。
