# 2026-11-05 线上变更：新增线上监控

## 授权与范围
- 操作时间／时区：2026-11-05 12:09–12:10（北京时间）
- 用户授权：马涛 2026-11-05 11:40 原话「线上服务器监控搞起来」
- 类型：新增监控脚本与 deploy 用户定时任务（不涉及应用代码、配置、服务）
- 允许修改的内容：只新增 deploy 家目录下的监控脚本、状态文件、通知地址文件和 deploy 用户 crontab 一行；不改 ExactCut 代码／配置／服务，不重启，不提权，不动 OSS／FBBM。

## 变更前后
- 应用 release：保持 `us-east-20261031-bottom-subtitles-8216c27`（健康接口实读），未重启。
- 新增文件：`~/exactcut-monitor/prod_monitor.py`、`README.md`；状态目录 `~/.exactcut-monitor/`（700），含 `state.json`、`monitor.lock`、`webhook.env`（600）。
- 凭据：只记录字段名 `FEISHU_ALERT_WEBHOOK`，存于 `~/.exactcut-monitor/webhook.env`（600），未写入仓库、日志或消息。
- deploy 用户 crontab：变更前为空，备份在 `~/exactcut-monitor/backup/crontab-before-20261105120932.txt`；新增
  `*/5 * * * * cd $HOME/exactcut-monitor && /usr/bin/python3 prod_monitor.py run >> $HOME/exactcut-monitor/monitor.log 2>&1`

## 执行与回退
- 结果：部分完成。服务存活与资源监控已上线；报错事件扫描未启用。
- 首次运行使用 `run --bootstrap`，只建立基线，不推历史。
- 已发送飞书消息：「ExactCut 监控测试消息」（云电脑本地发出）、「监控已上线」（线上发出），发送接口均返回成功。
- 回退：`crontab -e` 删除 `# exactcut-monitor` 注释行和对应 cron 行，再 `rm -rf ~/exactcut-monitor ~/.exactcut-monitor`。

## 验证与限制
- 首次检查：本机健康接口正常、心跳正常；公网首页 302 到登录页（视为正常）；服务进程存在；磁盘 / 使用率 26.5%；内存可用 94.8%；每核负载 0.12。
- 报错事件扫描不可用：应用日志目录 `/srv/exactcut/data` 为 root 0700，deploy 不在 docker 组，nginx 日志属 adm 组，journal 也看不到系统日志。需管理员另行授予只读权限（例如对日志目录加 deploy 只读 ACL），授权后脚本下一轮自动建立基线并启用，不推历史。
- 监控依赖本机 cron 和网络；主机整体宕机无法自报，需外部探测补充。

## 本地同步
- 对应文件：`monitoring/prod_monitor.py`、`monitoring/README.md`、`source/tests/test_prod_monitor.py`
- 分支：`feat/prod-monitor`，未合入 main。
