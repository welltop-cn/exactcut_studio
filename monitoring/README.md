# ExactCut 线上监控

单文件脚本，面向 Linux 普通用户 deploy；使用 Python 3.12 标准库，无第三方依赖，无需 root 或容器权限。文中的命令在监控脚本目录执行。

## 监控项与阈值

- 本机健康接口：默认回环地址的 18792 端口，要求返回 200、状态正常、工作进程存活、心跳不超过 120 秒。维护状态单独提示，不计故障。接口中的版本变化发送「版本已更新：旧→新」。
- 公网首页：默认 `https://autocut.creativefitting.ai/home/`，不跟随重定向，200–399 正常。
- 服务进程：读取 `/proc` 的命令行，匹配 `exactcut.webserver`。上述三项默认连续失败两轮才告警，恢复提示持续时间。
- 磁盘：根挂载点及额外配置的挂载点，可用空间口径的使用率超过 85% 告警，低于 80% 恢复。
- 内存：可用内存占比低于 10% 告警，高于 15% 恢复。
- 负载：一分钟负载除以逻辑核心数，超过 1.5 连续三轮告警，低于 1.0 恢复。五分钟轮询下约需十五分钟观察。
- 应用报错：默认扫描 `/srv/exactcut/data/logs` 中的 `events-YYYYMMDD-*.jsonl`。采集错误和严重错误级别；额外事件名白名单可采集警告。默认忽略两个 HTTP 失败事件中的断管和连接重置。
- 扫描无权限或目录不存在时，单独记录不可用状态，变化时提醒一次，其他检查继续；不在每条消息里重复。
- 每项检查隔离异常；本地仅记录中文检查名和异常类型，不输出异常正文。检查或发送连续三轮失败时尝试发送脚本自身异常，恢复后通知。

报错签名使用事件名、错误类型和首个堆栈位置；无堆栈时使用去除数字和标识的消息前 60 字。同一签名在一轮内合并，展示次数、上海时间的首次与最后时间、最多三个项目号、任务类型和 120 字摘要。所有输出链接均脱敏，摘要还会遮盖长十六进制标识和路径。同签名默认三十分钟只推送一次，期间累计次数会在下次推送时附上；即使没有新日志，到期也会推送累计次数。一轮最多一条报错合并消息，最多十类，其余仅显示类别数量；服务和资源通知另行发送。

## 安装与配置

把仓库的 `monitoring/` 内脚本复制到普通用户的 `$HOME/exactcut-monitor/`，例如在仓库根目录执行：

```sh
mkdir -p "$HOME/exactcut-monitor" "$HOME/.exactcut-monitor"
chmod 700 "$HOME/.exactcut-monitor"
cp monitoring/prod_monitor.py "$HOME/exactcut-monitor/"
cd "$HOME/exactcut-monitor"
```

使用安全编辑器在 `~/.exactcut-monitor/webhook.env` 保存下面的键及实际通知地址，不要提交该文件：

```text
FEISHU_ALERT_WEBHOOK=在此填写通知地址
```

```sh
chmod 600 "$HOME/.exactcut-monitor/webhook.env"
```

也可通过环境变量 `FEISHU_ALERT_WEBHOOK` 提供地址。文件内存在该键时优先使用文件；`--webhook-file` 可指定其他文件。脚本不会打印通知地址，发送超时为十秒，同时检查 HTTP 状态与飞书响应的 `code == 0`。

可选配置为脚本同目录的 `config.json`，或用 `--config` 指定；缺省配置无需创建文件。以下示例只有可公开的默认配置：

```json
{
  "host_label": "线上",
  "health_port": 18792,
  "public_url": "https://autocut.creativefitting.ai/home/",
  "process_match": "exactcut.webserver",
  "log_dir": "/srv/exactcut/data/logs",
  "mounts": [],
  "failure_count": 2,
  "heartbeat_seconds": 120,
  "disk_high": 85,
  "disk_low": 80,
  "memory_low": 10,
  "memory_high": 15,
  "load_high": 1.5,
  "load_low": 1.0,
  "load_count": 3,
  "cooldown_seconds": 1800,
  "warning_events": [],
  "ignore_errors": [
    {"event": "http.failed", "type": "BrokenPipeError"},
    {"event": "http.failed", "type": "ConnectionResetError"},
    {"event": "http.handler_failed", "type": "BrokenPipeError"},
    {"event": "http.handler_failed", "type": "ConnectionResetError"}
  ]
}
```

配置列表替换默认值；`ignore_errors: []` 关闭默认噪声忽略。阈值为严格大于或小于，边界和迟滞区间维持已有故障状态；非故障的连续计数在区间内清零。磁盘在通知里带挂载点显示，如「磁盘 /」。

## 首次运行与定时任务

先验证通知，再建立基线：

```sh
/usr/bin/python3 prod_monitor.py test-message
/usr/bin/python3 prod_monitor.py run --bootstrap
/usr/bin/python3 prod_monitor.py status
/usr/bin/python3 prod_monitor.py notify --text '监控已上线'
```

首次没有扫描基线时自动跳过历史日志，但仍会执行其他检查。显式 `run --bootstrap` 会将游标移至当前日志末尾，同时其他检查仅记录基线、清除旧的待发队列，不发送告警。之后新增的完整行才参与统计；末尾半行等完整后才处理。截断或文件标识变化从头读取，文件消失删除对应游标。

`--dry-run` 不调用飞书，把通知打印到标准输出；**仍更新状态、游标和限频时间**，演练请用 `--state-dir` 指定独立的临时目录。命令通用参数可以放在子命令前或后：

```sh
/usr/bin/python3 prod_monitor.py run --dry-run --state-dir "$(mktemp -d)"
/usr/bin/python3 prod_monitor.py test-message --dry-run --text '通知格式演练'
```

状态默认保存在 `~/.exactcut-monitor/state.json`；目录权限 700，状态文件权限 600，临时文件写入并同步后原子替换。文件锁覆盖整轮，重复启动直接跳过。发送失败的消息保留在状态队列，下轮重试。`status` 展示当前检查摘要、扫描状态及待发数量，不读取通知地址。

确认 `/usr/bin/python3` 是 Python 3.12 后，使用 `crontab -e` 加入：

```cron
*/5 * * * * cd $HOME/exactcut-monitor && /usr/bin/python3 prod_monitor.py run >> $HOME/exactcut-monitor/monitor.log 2>&1
```

标准错误由此进入 `monitor.log`；请使用用户可管理的日志轮转方式定期清理。消息时间固定使用 UTC+8，格式为 `11-05 11:40`，无需系统时区数据库。

## 回滚与限制

回滚：删除对应 cron 行，再删除 `~/exactcut-monitor/` 目录。若无需保留基线和通知配置，可另行删除 `~/.exactcut-monitor/`；保留它不会继续运行任务。

监控依赖主机、cron、网络和飞书可用，本机彻底宕机无法自报。普通用户看不到的进程或日志可能造成告警或扫描不可用，需要管理员预先提供读取权限。轮询间隔内已淘汰的文件无法补采；同一文件在两次轮询之间截断并重长至原大小以上时可能漏采。损坏日志行跳过并记录中文提示，不上传原文。文件较大或待发消息积压时单轮可能超过五分钟，文件锁会跳过重叠轮次。

最多十类的报错展示有意省略其余类别详情；冷却时间内不会重复提醒同类事件。失败重试采用至少一次语义，远端已接收但本地尚未保存时中断可能造成重复通知。状态损坏或配置无法读取时仅有本地错误日志，应通过外部存活监控补充。摘要脱敏采用规则匹配，应用日志中的自定义敏感业务字段仍需由应用侧治理；状态包含报错样本及标识，应保留目录访问限制。

离线回归使用 pytest 运行 `source/tests/test_prod_monitor.py`，测试不访问网络、使用临时日志和模拟发送函数。

## root 日志复制（log_feed）

应用每次写日志都会设置目录 `0700`、文件 `0600`，这会清掉 ACL mask 中的读取权限，导致即使预先配置 ACL，deploy 仍可能读不到日志。`log_feed.py` 由 root 的 cron 每五分钟只读扫描源日志，将新增 error/critical 完整行复制到 deploy 可读的独立目录；无需改动应用或源日志的权限。

### 安装

以下命令由管理员在仓库根目录执行。确认 `/usr/bin/python3` 为 Python 3.12，且 `deploy` 用户存在。脚本应放在 root 管理的位置，不能让 deploy 修改 root 将执行的脚本。

```sh
sudo install -o root -g root -m 0755 monitoring/log_feed.py /usr/local/sbin/exactcut-log-feed.py
sudo install -d -o root -g root -m 0700 /var/lib/exactcut-log-feed
sudo /usr/bin/python3 /usr/local/sbin/exactcut-log-feed.py --bootstrap
```

默认源为 `/srv/exactcut/data/logs`，输出为 `/home/deploy/exactcut-monitor/feed`。输出目录权限 `0750`，文件权限 `0640`，属主和属组取 `--owner deploy` 对应的 uid/gid。状态目录权限 `0700`，其中 `state.json` 权限 `0600`，保留在 root 私有目录，切勿交给 deploy。父目录必须预先存在或允许 root 创建，并确保 deploy 能遍历输出的父目录。目录的父路径及源目录应由可信管理员管理，不应允许不可信用户在任务运行时替换路径。

将 deploy 使用的 `config.json` 中 `log_dir` 设置为 `/home/deploy/exactcut-monitor/feed`，保留其他配置。先建立 prod_monitor 的扫描基线，再验证 root 复制任务及 deploy 监控任务正常运行。feed 文件名为 `events-YYYYMMDD-feed.jsonl`，兼容监控器的 `events-????????-*.jsonl` 扫描规则。

建立 root 定时任务文件 `/etc/cron.d/exactcut-log-feed`（权限 644）：

```cron
*/5 * * * * root /usr/bin/python3 /usr/local/sbin/exactcut-log-feed.py >> /var/log/exactcut-log-feed.log 2>&1
```

自行配置 `/var/log/exactcut-log-feed.log` 的日志轮转。两类 cron 的运行顺序不作保证，监控器可能到下一轮才看到本轮复制的数据。

参数均可显式指定：`--source`、`--dest`、`--state-dir`、`--owner`、`--retention-days`（默认 7 天）、`--bootstrap`、`--dry-run`。`--owner ''` 关闭属主切换，适合普通用户在临时目录离线演练；生产 root 任务应保留默认属主。三个目录不得相同或互为父子目录。示例：

```sh
/usr/bin/python3 monitoring/log_feed.py --source /tmp/feed-demo/source --dest /tmp/feed-demo/feed --state-dir /tmp/feed-demo/state --owner '' --dry-run
```

首次无状态时自动建立基线，或用 `--bootstrap` 主动跳过已有日志；均不复制历史。基线末尾半行的后续内容也跳过。`--dry-run` 只统计拟复制行数，不写 feed、不清理、不保存游标；会创建或收紧状态目录权限并使用锁文件。无状态的演练因此每次都只预览建基线，若要预览增量，应先在临时状态目录正常建基线，再追加临时源日志。

### 回滚

先删除 `/etc/cron.d/exactcut-log-feed`，再将 deploy 监控配置的 `log_dir` 恢复到原值或其他可读日志目录，然后删除 `/usr/local/sbin/exactcut-log-feed.py`。确认无需保留复制日志和游标后，可删除 `/home/deploy/exactcut-monitor/feed`、`/var/lib/exactcut-log-feed` 和复制任务日志。回滚不涉及应用代码或源日志；直接恢复源目录时，deploy 原有的读取权限问题仍需另行解决。保留状态目录可继续增量采集，删除状态则下次运行重新跳过历史。

### 限制

- 只接受源目录中的普通文件，以 `O_RDONLY | O_NOFOLLOW` 打开，跳过符号链接；不 chmod、chown 或改写源日志。读取可能由文件系统更新 atime。拒绝目标目录自身及输出文件为符号链接。此机制不隔离可信属主对 feed 的修改；deploy 能写自己拥有的副本，应将其视为监控输入而非审计存档。
- JSON 解析失败、非对象或级别不是 error/critical（比较时转小写）的行跳过；不复制 warning 白名单事件。保留整行原始字节，不脱敏，因此 feed 也可能包含业务敏感信息。时间取 `at` 的 UTC 日期；无时区按 UTC，日期解析失败用本轮当前 UTC 日期。
- 只推进完整换行的行，半行留到下轮。每轮最多复制 5000 行，达到上限后的内容留给下一轮；积压持续增长时需关注延迟。上限按行数计算，不限制单行字节数，也不限制扫描大量非错误行的耗时。
- 按文件名保存 inode 和字节偏移；inode 改变或文件变短时从头读取，文件消失时删游标。两轮间已删除的日志无法补采；文件截断后重长到原偏移以上或 inode 被复用可能漏采。源文件应仅追加并按文件轮转。
- 输出以 `O_APPEND` 写入，全部 flush、fsync 后才原子保存并同步状态；中断最多产生重复，不提前消费未同步的数据。状态损坏时退出报错，不自动重建基线。重叠运行因锁失败而非零退出。其他错误写中文 stderr 并非零退出，应监控 cron 日志。
- 只删除输出目录中 `events-*-feed.jsonl` 普通文件，按 mtime 严格超过保留天数清理；新追加的旧日期事件会刷新文件 mtime。保留期不是事件日期窗口，磁盘仍需独立监控。源日志淘汰、首次建基线和人工 `--bootstrap` 属于明确跳过场景；监控器自身首次建基线同样跳过历史，安装时应先完成两端基线。

离线验证：

```sh
python3 -m pytest source/tests/test_log_feed.py source/tests/test_prod_monitor.py -q
```
