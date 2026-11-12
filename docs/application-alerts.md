# 应用内告警与外部监控

本功能为 `feat/application-alerts` 分支实现，默认关闭。本文的服务器现状来自 2026-11-12 只读核验；本轮没有发布应用、修改生产配置/cron 或发送真实飞书消息。

## 已核实的现状

- 生产健康接口返回 release `us-east-20261111-recovery-7383715`、environment=production、status=ok、worker_alive=true；维护关闭。
- deploy crontab 每五分钟执行 `~/exactcut-monitor/prod_monitor.py run`。配置的日志目录为 `/home/deploy/exactcut-monitor/feed`；扫描状态正常、self_failures=0、待发消息为 0。
- `/etc/cron.d/exactcut-log-feed` 存在，feed 有三个日志文件。这里只确认文件存在与扫描状态，没有以 root 重新执行复制任务。
- `/home/deploy/.exactcut-monitor/webhook.env` 存在，权限 0600，`FEISHU_ALERT_WEBHOOK` 已配置，地址符合官方 HTTPS 机器人 v2 hook 路径。没有输出、下载或提交真实地址，也没有本轮发送验收。
- 应用当前主线已有只在创建日志时设置权限的修复；旧监控说明中的“每次 chmod 清掉 ACL”描述的是历史原因，不能据此推断当前应用仍有该行为。

## 新机制

`diagnostics.event()` 在脱敏后，将 error/critical 的有界上下文写入 `<data_root>/.web-state/alerts/queue.sqlite3`。HTTP 与后台子进程共享 SQLite 队列，提交时不访问网络；Web 主进程的后台发送器默认每五秒处理一次。即使日志文件写入失败，已入队的告警仍可发送；队列故障也不会阻止原诊断日志写入或改变业务结果。

- 沿用原监控的签名规则、30 分钟冷却和断管/连接重置过滤。同签名的重复次数持久累计，到期即使没有新事件也会汇总。
- 每轮新建一条合并通知，最多十类错误，最多 12,000 UTF-8 字节；放不下的类别保留到下轮。单类超长摘要标记截断。发送积压每轮最多尝试十条。
- 每个签名保留一个最新样例和最多三个关联项目 id。纳管执行器优先使用有效 `parent_batch_id`，缺失时从内部批次 `draft.json.media_internal` 解析父项目；内部批次另列。
- 展示已有的账号、项目、素材、请求/任务/诊断编号、阶段、版本、HTTP 路径、错误类型与栈位置。不存任意事件正文，URL 去掉凭据和 query/fragment，机器人地址与本机绝对路径遮盖。
- `interactive.failed` / `interactive.launch_failed` 补齐交互任务异常及线程启动失败事件；原制作、下载和交付 error 事件沿用现有入口。普通 warning/info 不报警，409 冲突不因 HTTP 状态本身报警。没有新增遍历所有业务状态的扫描器；仅记 warning 或完全没有诊断事件的其他业务路径仍需逐项治理。
- 通知先写入持久 outbox，成功后删除；HTTP 非 200、飞书业务 code 非 0、无有效回执或请求异常均保留待重试。唯一发送锁避免并发双发，网络请求期间不持有数据库事务。
- 默认关闭。真实发送同时要求 `EXACTCUT_ENVIRONMENT=production` 与现有 `EXACTCUT_ALLOW_EXTERNAL_WRITES=1`。只接受官方 `open.feishu.cn` / `open.larksuite.com` 的 HTTPS v2 hook，拒绝重定向。

## 配置

非密钥参数沿用 `util.load_config()`，由 runtime JSON 覆盖默认配置：

```json
{
  "alerts": {
    "enabled": true,
    "host_label": "线上",
    "public_url": "https://autocut.creativefitting.ai/home/",
    "cooldown_seconds": 1800,
    "poll_seconds": 5
  }
}
```

密钥使用已有应用凭据加载入口：在受限 `config/production.env` 中配置 `FEISHU_ALERT_WEBHOOK`。上线时可在服务器上从原监控的受限文件安全复制同一值，不放入 runtime JSON、仓库、shell 参数或验收报告。应用不会读取 deploy 家目录中的旧文件；容器也不需要挂载 feed 或 root 日志目录。

外部监控 `config.json` 新增 `"scan_logs": false` 时只关闭应用日志扫描与签名汇总，其他检查及原有待发消息重试保持运行；不清除旧状态。默认 true 保持旧部署兼容。

## 上线与回退

生产操作仍按 [部署规范](linux-migration.md)单独授权。建议先完成独立验收，再在维护/任务排空窗口切换：

1. 备份 runtime、production.env、旧监控代码/配置及两端队列状态。以实际服务器核对运行版本、活动任务、cron 和凭据权限。
2. 处理旧 feed 的最后增量并确认旧监控 outbox 已排空；旧 pending 签名需明确是等待旧冷却到期发送还是接受切换时不补历史。应用内新队列不会导入历史日志或旧签名。
3. 为应用配置同一 webhook 与 `alerts.enabled=true`；将外部监控更新为支持 `scan_logs` 的版本并设置 false。通过发布管理器发布已提交的干净版本，避免新旧应用报错通知长期重叠。
4. 在授权范围内写入一条明确标识为测试的应用诊断错误，确认由应用后台送达真实飞书群，核对父项目链接、聚合、队列清空、服务与 worker 健康。此步骤本轮尚未执行。
5. 新链路验收通过后由管理员停用 root 的 log_feed cron。保留原脚本、配置和 feed/状态供回退；deploy 的存活与资源监控 cron 继续保留。

回退时先关闭应用 alerts，通过发布管理器恢复已封存代码；恢复外部 `scan_logs=true` 和 root 复制任务。保留最新业务数据库及两边告警队列。回退到不认识 alerts 的旧代码不会自动发送新 outbox；需明确处理该积压，不能宣称完整补发。旧 scanner 使用原游标，避免不经确认 bootstrap 跳过增量。

## 限制与运维

- 至少一次发送：远端已收消息但本地未删除时进程中断，可能重复；不承诺恰好一次。
- SQLite 争锁等待上限 50ms，但磁盘 I/O 没有硬实时保证。争锁、磁盘满、损坏数据库或签名容量满时，当前事件可能入队失败；业务继续，stderr 最多每分钟一条 `alert_failed type=...`，不记录异常正文或密钥。原诊断日志仍尽力写入。
- 最多 1,000 个签名、100 条待发通知；队列满时已有签名继续累计，待发消息不删除，超过签名容量的新签名拒绝入队。无 pending 的签名在后续入队时按一天回收；数据库文件可能保留 SQLite 已分配空间。
- 初始目录 0700、数据库与锁文件 0600；状态含业务标识，应按生产私有数据管理。拒绝直接使用符号链接的告警目录/数据库/发送锁，父目录依赖既有可信数据根权限。
- 发送器不在健康接口里增加“飞书可达”判断。长期发送失败、队列初始化失败目前通过 stderr 排障，不能把服务 health=ok 当作群通知正常。
- 进程退出、整机故障和网络中断不能依赖应用自己通知。外部原有健康、进程、公网与资源检查继续运行；整机彻底宕机仍需异地主动探测。

## 隔离验证

```sh
cd source
python3.12 -m pytest -o addopts='' \
  tests/test_application_alerts.py tests/test_interactive_queue.py \
  tests/test_prod_monitor.py tests/test_diagnostics.py tests/test_mosaic_queue_release.py \
  tests/test_interactive_main.py -q -ra
```

测试使用独立目录、合成错误和模拟通知边界，包括跨进程累计、真实 Web 主入口的后台线程生命周期、网络失败重试、重启恢复、冷却、合并及脱敏；不访问真实飞书、AI、OSS 或 FBBM。具体结果见 [审查与验证记录](reviews/application-alerts-review.md)。
