# Linux 部署与迁移约定

> 2026-10-10：本文网页入口示例统一为新默认 `/home`；历史验收记录不代表该路径已经上线。现有生产 runtime 与 nginx 的迁移由部署方处理，本次仅修改开发代码和文档。Cookie Path 改为 `/home/` 后，已登录用户需重新登录一次。

首期只维护两份实际密钥配置：本地 `.env.development` 和服务器 `<部署根>/config/production.env`。Docker 中的应用不依赖 Mac 路径、Cloudflare Tunnel、宿主 Python 或宿主字体。Linux 运维脚本使用系统 Python 3.10+ 和 Docker/Compose；同架构机器可迁移已验收镜像，跨架构需要重建并重新验收。

## 配置与数据

默认部署根为 `/srv/exactcut`，运维命令的 `--root` 可更改。IP 仅用于 SSH 和用户访问，不写入可复用代码。

```text
<部署根>/
  config/production.env       # 唯一生产密钥配置，0600
  config/runtime.json         # 非密钥：生产运行配置、磁盘策略
  config/deployment.json      # 非密钥：实例、端口、CPU/内存
  data/                      # 持久化账号、批次、素材、正片和缓存
  bin/linux-ops              # 发布、回滚、恢复、nginx 配置生成
  bin/linux-preflight        # 外部服务检查
  releases/<版本>/            # 固定镜像ID、提交、Compose、测试日志及校验摘要
  current.json               # 当前版本与上一版本
  history/                   # 每次切换的成功或失败记录
  preparation/               # 隔离验收；不是正式发布
```

两个 JSON 文件沿用应用配置机制或保存机器参数，不是额外的 `.env`。密钥与运行配置只读挂载到 `/run/exactcut/`，数据挂载到 `/data`；迁移只需调整部署根和机器参数。Compose 明确使用 `--env-file /dev/null`，不自动加载旧 `.env`。

开发仍使用 `.dev-state/data` 和 8793，不加载生产 `runtime.json`。生产 Compose 完整启用下载、AI、剪辑、飞书/FBBM 发布；发布保留工作台预览确认流程。

## 磁盘策略

`source/config/production.json` 是无密钥模板，首次初始化复制到服务器 `config/runtime.json`。本机生产模板预留 10 GiB，正片缓存与基础渲染缓存各 8 GiB，基础缓存保留 7 天，可用空间低于 16 GiB 时按需回收可回收正片。下载按每集 2 GiB 估算所需空间；引用中或被锁定的正片不清理，容量不足则拒绝开始新下载。

这些是任务开始前的空间检查和缓存回收策略，**不是文件系统硬配额**。超出估算的视频、渲染临时文件、已发布成片和其他服务仍可能消耗磁盘；需要监测实际可用空间。不会为满足上限删除账号、数据库、批次或成片。换更大机器时修改此 JSON，不修改本地开发默认配置；重启应用前先等待任务完成。

## 发布与回滚

安装脚本：

```sh
sudo python3 source/deploy/linux_ops.py --root /srv/exactcut init \
  --runtime-config source/config/production.json \
  --instance exactcut-production --backend-port 18792 --public-port 80 \
  --cpus 1 --memory 2g
sudo install -m 755 source/deploy/linux_ops.py /srv/exactcut/bin/linux-ops
sudo install -m 755 source/bin/linux-preflight /srv/exactcut/bin/linux-preflight
```

初始化拒绝覆盖已有机器配置或混用其他环境数据。通过安全渠道提供 `config/production.env`，权限 0600；不得把密钥提交到 Git。

```sh
sudo /srv/exactcut/bin/linux-ops --root /srv/exactcut build <版本> --repo <干净工程仓库>
sudo /srv/exactcut/bin/linux-ops --root /srv/exactcut publish <版本>
sudo /srv/exactcut/bin/linux-ops --root /srv/exactcut status
sudo /srv/exactcut/bin/linux-ops --root /srv/exactcut rollback
sudo /srv/exactcut/bin/linux-ops --root /srv/exactcut rollback <指定版本>
```

构建只接受已提交且干净的仓库，拒绝真实环境文件、业务目录、数据库、视频和链接。构建时标记提交，运行无生产密钥/数据的 Linux 全量回归，然后记录固定镜像ID、提交、schema 1、测试结果和文件摘要。验证镜像内的提交标签，禁止覆盖版本目录。测试镜像单独安装 Git，不将它加入生产镜像。

切换前进入维护状态，等待现有请求和任务结束；超时退出，不强制杀任务。发布后检查 `/healthz` 的 status、production 环境、版本和 worker。失败自动恢复前一版本，**代码回滚保留当前数据库和业务数据**。首次发布失败删除自己的失败容器，以便重试；同名未登记容器禁止接管。若恢复也失败，保留维护状态，排除原因后运行 `linux-ops --root <根> recover`。

生产 `config/runtime.json` 的 `web.base_path` 设为 `/home`（不带末尾斜线），开发默认值同为 `/home`。`linux-ops --root <根> nginx` 据此生成 location 片段，安装为 `/etc/nginx/snippets/exactcut-production.conf` 并在已有80端口 server 块内 include；保留 `/paypal_dispute/` 和 `/domain-watch/`。访问入口为 `http://<IP>/home/`，后端仍仅监听本地18792，不新增公网端口。`deployment.json` 的 `public_port` 为80。nginx保留前缀传给应用；应用统一处理登录跳转、静态资源、API、Cookie和视频Range。安装前用 `nginx -t` 验证，再 reload。

## 可重复的外部验证

```sh
sudo /srv/exactcut/bin/linux-preflight \
  --root /srv/exactcut --image <固定镜像ID或验收镜像> \
  --drama-id <已存在的六位剧ID> --sample-mib 32
```

临时容器只读挂载生产配置，不挂载业务数据、不监听端口、不启动 worker。OSS 对已有 EP1 做 HEAD 和限量 GET，读入内存后丢弃；不调用可能补写目标桶的 `fetch-episode`。AI 使用实际配置模型验证小型 Responses 结构化输出；飞书只鉴权并读取字段与状态选项；CF-Ads 只登录并读取 `app_usage`。报告不打印凭据、token 或接口错误正文。

读取通过不代表 OSS 上传、飞书写权限或 FBBM 发布已验收；AI 小型请求通过不代表全部业务场景已验收。真实长视频、多集和发布仍需明确素材范围并经工作台预览确认。

## 换机器

1. 检查新机 Docker/Compose、磁盘、资源和端口，准备 nginx 的路径入口。
2. 旧机停止接新任务，等任务完成并停止应用，再一致性备份 `config/`、`data/`、版本记录和脚本，保留权限。整个迁移过程仅一台机器执行任务。
3. 使用 `docker save <固定镜像ID> -o exactcut-image.tar` 与 `docker load -i exactcut-image.tar` 搬迁同架构已验收镜像，同时搬迁 `releases/`、`current.json`、`history/` 和 `bin/`。
4. 恢复到新根，调整 `config/deployment.json` 的端口和资源；保留 production 数据归属与 schema 1，生产配置权限 0600。
5. 在新机运行同一 `linux-preflight`，验证账号、批次、长视频、多集和 QC。
6. 用 `linux-ops --root <新根> recover` 启动登记的当前版本并检查健康。失败排查时不得还原旧业务数据库。
7. 切换 IP/域名入口，旧机保持停机，确认业务正常后再结束迁移。

服务器规格、CPU/内存/磁盘预算与实测依据见 [服务器配置建议](server-sizing.md)。首期独立机器建议4vCPU/8GB/100～200GB SSD；本次共享机仍使用1核/2GiB应用限制。
