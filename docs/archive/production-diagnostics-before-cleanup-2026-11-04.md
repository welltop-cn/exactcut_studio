# 生产问题定位日志

> 历史归档（2026-11-04）。正文保留当时的方案或执行状态，不代表当前版本；日常操作见[文档入口](../README.md)。

需求依据：[试用反馈文档](https://welltop.feishu.cn/docx/IpVodvkPuol1yJx9TedcxVxKnph)，读取 revision 4（2026-10-10）。这些是历史症状，新增日志不能补出历史根因，也不代表原问题全部修复。

## 保存与读取

应用 JSONL：宿主 `/srv/exactcut/data/logs/events-YYYYMMDD-*.jsonl`，容器 `/data/logs/`。Web、阶段任务、预下载进程共同写入，进程间锁保护整条写入与轮转；目录 0700，文件 0600。UTC 日期分文件，单文件 20 MiB，默认最多保留 14 天，应用日志总量 1 GiB，先达到容量上限时会提前淘汰最旧文件。清理由写入触发，空闲 worker 每分钟记录一次状态；服务完全停止时不执行清理。

集中配置 `config/runtime.json` 的 `logging` 段覆盖 `config/default.json`：`enabled`、`retention_days`、`max_file_bytes`、`max_total_bytes`；本轮新增 `browser_events_per_minute`（默认240，按账号统计，与客户端ID无关）。修改后重启生效。日志策略不参与成片缓存指纹。原有任务日志/审计 JSON 保留，**不受新日志的容量上限管理**。

反向代理补充 JSON 日志：`nginx-access.log`，记录不带 query 的路径、HTTP 状态、总耗时、upstream 耗时/状态、应用 trace ID。单独 logrotate：每日/20 MiB 轮转，最多 14 份、14 天，小时检查；高流量时实际保留时间缩短。应用日志和代理日志是两套限额。

查询无需新增依赖，在服务器运行（容器名先用 docker compose/ps 查实际值）：

```sh
docker ps --filter label=com.docker.compose.project=exactcut-production --format '{{.Names}}'
docker exec <容器名> python -m exactcut.diagnostics --hours 2 --errors --limit 100
docker exec <容器名> python -m exactcut.diagnostics --batch <项目ID> --hours 24 --limit 300
docker exec <容器名> python -m exactcut.diagnostics --job <任务ID> --hours 24
docker exec <容器名> python -m exactcut.diagnostics --trace <X-Request-ID> --hours 24
docker exec <容器名> python -m exactcut.diagnostics --batch <项目ID> --request R001
docker exec <容器名> python -m exactcut.diagnostics --actor producer03 --hours 2 --limit 300
docker exec <容器名> python -m exactcut.diagnostics --client <浏览器请求ID> --hours 2
docker exec <容器名> python -m exactcut.diagnostics --page <浏览器页面ID> --hours 2
docker exec <容器名> python -m exactcut.diagnostics --event hook_preview.admission_denied --hours 2
```

## 2026-10-12 诊断补全（本地，尚未上线）

收到错误截图先查弹窗底部的“诊断编号”：它等于错误JSON的trace_id和HTTP响应X-Request-ID。现有前端显示error_info.detail，因此无需浏览器开发者工具即可提供编号。`--trace`同时检索服务器trace_id与browser.http_trace_id；再沿batch_id/request_id/job_id查任务阶段。没有编号时按账号和反馈时间查询，日志使用UTC，北京时间须减8小时。

| 用户现象 | 新增证据 | 用法 |
| --- | --- | --- |
| 保存、确认、复制、字幕预览或上传点击后无响应 | browser.event/action、request_started、request_completed、network_error | 真实按钮ID或固定动作名、项目ID、素材行号、禁用状态；通过client_request_id区分请求未到服务端、处理未结束和响应丢失 |
| 字幕预览资源繁忙 | hook_preview.admission_denied | reason区分preview_pool_full与disk_reservation，记录active_previews/preview_limit；不再只能依赖相邻一分钟的调度快照 |
| 字幕预览慢或AI服务繁忙 | hook_preview.compute/emphasis/sampling/presentation、ai.deferred | 分别定位整体计算、重点分析、抽帧、画面排版的耗时；原有AI预算、HTTP状态、重试日志沿用 |
| 预览计算完却没显示 | hook_preview.result_rejected、http.rejected | 区分计算期间草稿变化、归档、维护、权限变化或AI延期；记录保存的revision与请求revision |
| 视频无法播放/卡住 | browser.event/media_error、media_waiting、media_stalled、media_playing | 素材request_id、media_error代码、readyState/networkState、播放时间及前后台状态；与preview.stream/OSS/代理日志对照，不采集视频URL |
| 页面版本过旧、同账号开多个标签页 | browser.page_id、frontend_build、page_ready/visibility | 页面实例ID区别标签页，frontend_build对应app.js文件内容哈希；服务端release仍记录实际代码版本 |

日志新增不改变重试策略：第4个并发字幕预览仍需稍后重试，网络完全断开时浏览器上报同样可能失败。客户端事件是线索，不能单独证明操作已执行或素材已交付。用户界面里没有服务端请求的本地校验错误不自动获得服务端诊断编号。

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

- 正常健康探测不逐条写应用日志；常规 GET 轮询每路由/用户 30 秒采样；排队原因每任务/原因 60 秒采样。业务HTTP错误、修改请求、慢请求完整记录。成功的client-events只记录browser.event，避免上报本身重复制造HTTP日志。
- 浏览器每分钟常规事件最多40条、错误事件独立最多40条，时间窗到期自动恢复，避免旧版累计20条后长期开页完全失去日志。服务器先按登录账号执行每分钟240条硬上限，再按账号/页面/动作/项目/路由/状态/客户端请求等维度10秒去重；不同请求或操作不会被同一个粗粒度键合并，伪造不同客户端ID不能绕过账号上限。超过上限时返回成功回执但丢弃上报，每窗口只记录一次browser.rate_limited，避免限流本身制造海量HTTP错误。page_id、client_request_id都是诊断标识，不用于授权。
- 浏览器报告只含枚举、数字和受限标识，不含错误正文、页面内容或表单；它是不可信客户端证据，必须对照服务器事件。浏览器完全离线时无法上传报告；不能用“没日志”直接认定没有点击。
- 不记录请求/响应正文、AI prompt、Cookie、认证 header、完整命令行或带签名的 URL；异常记录类型、脱敏消息与栈位置，不包含局部变量和源码行。下载错误只保留有界尾部。
- 日志写入失败或锁等待超时不会让制作失败；stderr 每分钟至多报告一次 diagnostic_write_failed。监控此信息：日志系统故障时不能保证证据齐全。追加已 flush，但不逐行 fsync；机器突然断电可能丢失最后部分日志。
- OOM/SIGKILL 无法由被杀进程自行记日志。结合 Docker 状态、`docker inspect` 的 OOMKilled/ExitCode，以及主机 kernel journal；最后一条 started 没有 completed 只是定位线索，不是根因结论。
- 画面审美、违规镜头判断、希望新增字体/ending 等产品需求不能靠日志证明“正确”；日志用于确认输入、执行版本和失败环节，视觉结论仍需看素材。

## 部署代理日志

将 `source/deploy/nginx-observability.conf` 安装到 nginx http context；只在 Exact Cut 的 server 中添加 `access_log /srv/exactcut/data/logs/nginx-access.log exactcut_diagnostic;`。备份配置，先 nginx -t 再 reload。logrotate 模板安装到部署根 config，使用独立状态文件每小时调用，避免与系统 logrotate 重复管理。保留原 nginx error log 用于 upstream 连接失败等系统诊断。

## 2026-10-27 剪切和打码播放诊断（已正式发布）

剪切原片/结果、打码原片/结果/片段预览接入既有 `media_error`、`media_waiting`、`media_stalled`、`media_playing` 事件。`source` 为播放器 DOM ID，`session_id` 为编辑会话 ID，`batch_id` 和 `request_id` 取播放器所属项目和素材，避免切换项目后误关联。新增 `paused`、`seeking`、`ended` 为 0/1；保留 `ready_state`、`network_state`、`media_error`、`current_ms`。

两个自定义播放按钮的 Promise 拒绝上报 `media_play_rejection`，`error_type` 只记录异常名称（如 AbortError），以 warning 入日志；会话归属在点击时捕获，播放器状态在拒绝时读取。延迟拒绝时播放器可能已经关闭或重新打开，应结合后续事件判读。诊断异常不会改变原有播放或提示逻辑。

排查先按 `--batch <项目ID> --hours 2` 查询，再按 `browser.session_id`、`browser.source` 和时间对应 `/mosaics/<会话>/source` 或 `/cuts/<会话>/source` 的 `http.rejected` 与 nginx 请求。409 的原因若为“当前成片已变化”，说明旧会话版本检查拒绝；不能据此解释更早的播放失败。206 或应用侧写入完成不证明客户端收到并成功解码。

继续沿用客户端分钟额度、服务端账号额度和 10 秒去重，去重键加入 session_id；不采集 URL、异常正文或凭据。已随 us-east-20261027-playback-a5f7633 正式发布，已有页面需刷新后才使用新日志；完全离线时上报仍可能丢失。详见 [正式发布记录](verification/playback-production-release-2026-10-27.md)。
