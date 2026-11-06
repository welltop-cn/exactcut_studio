# 2026-11-06 线上变更：监控告警补充排障上下文

## 授权与范围
- 操作时间／时区：2026-11-06 11:33–11:34（北京时间）
- 用户授权：马涛 2026-11-06 11:32 原话「部署，同时开分支让应用日志补项目名、方法、状态码、耗时和上游地址」
- 类型：监控脚本更新（不涉及应用代码、配置、服务）
- 允许修改的内容：仅覆盖 deploy 家目录 `~/exactcut-monitor/prod_monitor.py` 与同目录 `README.md`；不改 crontab、webhook.env、config.json、feed、应用服务；不用 root。

## 变更前后
- 应用 release：保持 `us-east-20261031-bottom-subtitles-8216c27`（未重启）。
- 监控脚本：由旧版（sha256 `75ec1e0214bdf84ce88570902b12fa0d8feb792378e3edfa267a950d1127029d`）更新为 `fix/prod-monitor-context` 提交 `5fd750c` 对应文件（sha256 `594ab0d34e5757a2d01e6786fd26c1cb477e7270d3c9cd27e70605947a58a885`）。
- 行为变化：报错合并告警每组保留多行样例，含完整项目 id、项目深链、账号、素材、关联 id、请求路径、主机、日志文件名、末帧栈与较完整错误信息；URL 去掉 query／签名。
- 凭据：未改动 `~/.exactcut-monitor/webhook.env`；未记录 webhook 明文。

## 执行与回退
- 结果：成功。
- 备份：`~/exactcut-monitor/backup/prod_monitor.py.20261106113344`、`~/exactcut-monitor/backup/README.md.20261106113344`
- 回退（deploy，无需 root）：
  ```sh
  cd "$HOME/exactcut-monitor"
  cp -a backup/prod_monitor.py.20261106113344 prod_monitor.py
  cp -a backup/README.md.20261106113344 README.md
  ```
  crontab 与状态目录无需改动。

## 验证与限制
- `python3 prod_monitor.py status`：报错扫描正常；本机服务／公网首页／进程正常；磁盘约 27.4%；内存可用约 94.7%；每核负载约 0.07。
- 格式验证：在独立临时 state-dir＋feed 副本上 `run --dry-run`（不发飞书），确认新告警含项目深链与完整 id；并校验 `~/.exactcut-monitor/state.json` 的 sha 与 mtime 未变（避免 dry-run 推进游标／冷却导致漏告警）。正式 cron 仍用原状态目录，未对其执行 dry-run。
- 未覆盖：应用侧仍缺项目名、HTTP method／status／duration_ms、上游下载地址；这些另开分支补日志，本次未部署应用。

## 本地同步
- 对应提交：`fix/prod-monitor-context` 的 `5fd750c`（告警脚本）及本记录提交。
- main 合入状态：未合入；未 push（以当时仓库操作为准）。
