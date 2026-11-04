# Linux 部署、回滚与迁移

应用通过 Linux/Docker 发布管理器部署。本文按当前仓库工具核对；具体机器、监听、容量和运行版本以部署配置及发布管理器实读为准。历史 Mac、Tunnel 和初次服务器迁移记录已归档。

生产变更需要用户明确授权。先在独立环境验收；合入 main、构建成功或测试实例通过，均不表示生产已经发布。

## 配置与持久数据

| 部署根下的路径 | 用途 |
| --- | --- |
| `config/production.env` | 受限的生产凭据，权限 0600 |
| `config/runtime.json` | 非密钥运行配置，通过集中配置加载 |
| `config/deployment.json` | 实例、端口及 CPU/内存等机器参数 |
| `data/` | 持久账号、项目、媒体、任务与缓存 |
| `releases/<版本>/` | 固定提交、镜像、校验摘要和验证结果 |
| `current.json`、`history/` | 当前/前一版本及切换记录 |
| `bin/` | 已安装的运维入口 |

代码、发布目录和业务数据分开。生产密钥、源片与成片不进 Git；开发只使用独立配置和数据。Compose 的配置边界见 [docker-compose.production.yml](../source/docker-compose.production.yml)。

`source/config/production.json` 是模板，不是线上实际配置。更新代码不会自动覆盖服务器 runtime；复制旧模板也可能丢失后续配置变更。CPU、内存、线程和并发容量需按目标机器验收，不照搬旧部署数字。

默认网页前缀由 `web.base_path` 配置，当前源码为 `/home`。反向代理保留此前缀，后端仅监听本地；不要通过新增公网后端端口替代代理入口。

## 状态核对与发布

下面命令从仓库根执行，用实际部署根、干净工程路径和唯一版本号替换尖括号参数。已安装的 `bin/linux-ops` 提供同一入口。

```sh
python3 source/deploy/linux_ops.py --root <部署根> status
python3 source/deploy/linux_ops.py --root <部署根> build <版本> --repo <干净工程仓库>
python3 source/deploy/linux_ops.py --root <部署根> publish <版本>
python3 source/deploy/linux_ops.py --root <部署根> status
```

构建只接受已提交且干净的工程，固定镜像身份，并在独立验证镜像中执行完整测试。运行镜像只安装运行依赖，测试工具在验证镜像内。版本目录不可重复覆盖。

发布进入维护并等待已有请求和任务结束，超时退出，不强杀制作任务。切换后检查健康、环境、版本、worker，再进行已授权范围内的页面及业务验收。记录写入[线上变更索引](production-change-log.md)。健康成功不等于真实成片或交付已经成功。

首次新建独立部署才使用 `init`，已有部署不要重复初始化：

```sh
python3 source/deploy/linux_ops.py --root <部署根> init \
  --runtime-config source/config/production.json \
  --instance <独立实例名> --backend-port <本地端口> --public-port <代理端口> \
  --cpus <已验收CPU数> --memory <已验收内存>
```

通过安全渠道提供凭据，检查数据归属及配置挂载。代理配置由 `nginx` 子命令生成，再按目标机器检查、安装；修改代理前保留配置，验证通过后才 reload，不覆盖其他服务的规则。

## 回滚与恢复

```sh
python3 source/deploy/linux_ops.py --root <部署根> rollback
python3 source/deploy/linux_ops.py --root <部署根> rollback <指定已封存版本>
python3 source/deploy/linux_ops.py --root <部署根> recover
```

健康失败会尝试恢复前一版本；恢复也失败时保留维护状态，排除原因后再执行 recover。普通代码回滚保留最新数据库、项目、成片和交付记录，不恢复旧业务数据，也不会撤回远端素材。

回滚前检查持久协议及依赖兼容性。存在候选任务或待核实交付时，按管理器的阻塞原因逐项处理，不能直接删状态或重复上传来解除阻塞。具体命令参数见 [linux_ops.py](../source/deploy/linux_ops.py) 的 CLI 定义。

## 预检、磁盘与迁移

`source/bin/linux-preflight` 检查已有原片读取、AI 小型调用及外部服务连接，使用前核对其 `--help` 和授权范围。它可能发起真实 AI 请求；只读检查通过不代表 OSS 写入、飞书建记录或 FBBM 最终交付通过。

`storage` 配置用于任务准入、磁盘预留及可回收缓存策略，不是文件系统硬配额。容量不足时拒绝新任务，不为腾空间删除账号、项目、成片或在用原片。

迁移机器时：

1. 核对新机架构、Docker/Compose、容量、端口及代理。
2. 停止旧机接收新任务，等待任务结束并正常停服，取得配置与数据的一致备份。
3. 搬迁配置、数据、固定镜像、发布记录及运维入口，保留权限；跨架构重建并重新验收。
4. 调整机器配置，以登记版本恢复；检查账号、项目、实际媒体、日志与外部连接。
5. 确认新机业务可用后切换入口；整个过程只允许一台机器执行同一份业务数据。

历史部署与规格测量在[归档](archive/README.md)中，不能据此认定当前服务器仍是相同版本、资源或入口。
