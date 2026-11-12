# 本地开发与专项服务清单

整理日期：2026-11-04。这里保留默认开发约定和最近专项实例；旧端口、PID、启动脚本流水账见[历史清单](archive/local-dev-services-before-cleanup-2026-11-04.md)。

## 2026-11-12：AI 重点自动重试独立 dev

- 用户要求本地独立 Chrome 窗口开启 dev；入口 `http://127.0.0.1:8793/home/#cab91483e510401093b192ad7550c470`，已登录新数据根的本地管理员。
- 独立代码 worktree：`/Users/matao/exactcut-test/hook-validation-70c7c47/code`，固定提交 `70c7c47e6c532cd892ad559a415fade382058872`，对应 `fix/hook-validation-auto-retry` 的业务修复。不要在该运行树切分支或修改源码。
- 使用原 `source/bin/dev-web` 和约定 Python 3.12；独立数据为该 worktree 的 `source/.dev-state/data`，密码只保存在其中 `.web-state/password`。启动日志 `/Users/matao/exactcut-test/hook-validation-70c7c47/dev.log`。初次核验 PID 32148，操作前必须重新检查监听与 cwd。
- 环境 development、release development、worker_alive=true，maintenance=false；无普通制作任务。新数据只有初始草稿项目，未复制生产素材、账号或回执。真实 AI / 素材下载 / 发布和外部写入均沿用启动器的关闭设置。
- Chrome 独立窗口已实际加载工作台、登录并显示“已连接”；此步骤只验证 dev 可访问，不代表真实 AI 制作链路验收。代码已另通过 308 项相关全部层回归和 1657 项默认单元测试。
- 初次默认 dev 之后，用户明确要求实际在 dev 测试。确认无活动任务后，仅将本实例切为私有 `serve_fixture.py`，release `dev-hook-fixture-70c7c47`；应用源码不变。使用 development 环境和本地模型模拟，AI 功能开关仅用于触发真实校验路径；无真实 AI 凭据。集中配置关闭 OSS 预览与标题提取，对外写入、下载、交付继续关闭。新 PID 初次核验 17961，操作前仍须重新核对。
- 已由 Chrome 实际点击制作并完成受控验收：自动重试成功、重试耗尽停止、服务错误不重制、单条修改时另一条成功素材版本/文件不变。两条真实成片播放成功，最终显示“1/1 条制作完成 · 1 条沿用已有成片”。[完整证据和夹具边界](verification/hook-validation-dev-chrome.md)。
- 当前 Chrome 停在 `http://127.0.0.1:8793/home/#5a23f54ac32d4bffa6084869bf6d5623` 的成片预览；默认草稿保留。初次夹具缺少默认水印字段引起整批代际变化的未通过观察也已保留；补齐夹具默认值后重跑的成功素材保护通过。
- 原 18794 / 18895 测试服务继续保留，未重启、未修改其数据。本实例按用户要求留给人工操作；后续停止时只操作重新核实的 8793 实例。

## 2026-11-11：下载完整性与恢复修复测试实例

- 新本地隔离 dev：`http://127.0.0.1:18895/home/#f2f245e0f51e4c6c91f4fee42a1fff88`，运行代码 `74b065a`，release `dev-recovery-review-74b065a`；原 18794 服务保留。
- 服务器沿用 `http://8.221.122.75/test/#81aeef49800d4961b57b9dbcd46ec935`，release `server-test-recovery-review-74b065a`。入口原本仍在 `/etc/nginx/conf.d/exactcut-test.conf`，不是 sites-enabled 中的正式域名配置；只更新该测试入口的 upstream。
- 两处均为独立数据/账号与合成字节上游，不是生产项目副本，也不是实际视频素材。测试适配器仅在私有启动器中替换目录/签名及本地源路由，真实 HTTP 代理、下载 UI 和文件保存函数不替换；外部写入、AI、发布及真实素材获取仍关闭。
- 独立验收 Chrome CDP 18897 只对 `http://8.221.122.75` 启用 insecure-origin-as-secure，以覆盖 HTTP IP 上的目录保存分支。真实 OPFS 目录句柄存入应用原有目录记录，然后点击页面下载按钮；没有冒充系统目录选择器验收。
- 本地和服务器各三项实际点击通过：EP1 短正文被拒绝，EP2 保存 16 字节，EP3 压缩响应 502；两种失败均保留 8 字节旧文件。截图与最终文件核对保留在 `/Users/matao/exactcut-test/download-integrity-fix-20261111/` 的 `visible-local.png`、`visible-server.png`、`visible-final-state.json`。
- 私有启动器 `visible_instance.py`、本机数据 `visible-local-data/`、PID 和日志均在上述目录；服务器对应目录为 `/srv/exactcut/preparation/download-integrity-fix-20261111/`，数据 `visible-data/`，容器及 internal 网络均命名 `exactcut-download-visible-20261111`，限制 1 CPU / 2 GiB。端口和 PID 操作前重新核对。
- 后续修复下载中改剧号/切项目导致混存，以及选择目录期间重复启动；两处各 81 项回归通过，见[验证记录](verification/original-download-context-2026-11-11.md)。本机当前源码快照为 `/Users/matao/exactcut-test/recovery-review-20261111/after/source`，服务器为 `/srv/exactcut/preparation/recovery-review-20261111/after/source`。修改主仓库不会自动更新这两份快照；测试数据与私有启动器仍沿用上一轮目录。
- 本轮又修复单条损坏报告阻塞交互队列；两处各 321 项相关回归通过。空闲检查后更新测试代码，旧服务器测试容器 `exactcut-download-visible-20261111-8d7234e` 停止保留。健康检查、代码哈希与生产未变核对见[恢复链路跟进](reviews/retrospective-followup-recovery-2026-11-11.md)。坏报告注入仅在独立回归临时目录中进行，未改可见测试项目的报告；本轮未重复点击下载验收。
- 上述测试实例更新时生产保持 `2fb979a`，仅历史 IP 测试 upstream 调整做过 Nginx graceful reload。随后按独立发布授权，生产已于 2026-11-11 21:51 更新至 `us-east-20261111-recovery-7383715`，镜像 de5f35fc…；见[生产发布验收](verification/production-recovery-download-2026-11-11.md)。本表的两处测试实例仍为 `74b065a`，应用代码与该发布一致。旧 `exactcut-test` 容器及数据保留，未恢复其旧后台任务。

## 2026-11-10：Hook / 上传修复的本地配置复现

- 本机 `http://127.0.0.1:18794/home/` 已启动，代码 `440bd37`，release `development-local-440bd37`；PID 启动时为 36972，后续操作须重新核对。
- 沿用 `/Users/matao/exactcut-test/env.sh` 的独立本地数据、测试 OSS 命名空间及交付 guard；使用 `/Users/matao/miniconda3/envs/py3.12/bin/python3.12`。日志和 PID 在 `/Users/matao/exactcut-test/bug-replay-20261110-440bd37/`。
- 用户要求只复制两个报错项目的配置，并在本地走制作流程。实际仅导入 15,432 字节的草稿配置，未复制生产原片、成片、账号、任务状态或交付回执。生成新项目和新 request_id，清除历史预览凭证。
- 本地项目：`9384eae8db5940a89760c138766bc5f4`（101689，4 条）和 `bca42906b2b649adaae9f0778c269c26`（101708，5 条）。初始均为 draft；本地尚无对应原片，需要在页面准备原片后制作。未自动点击制作。导入后曾误归属 admin，导致当前 verifier 账号列表不显示、详情返回 404；现已将这两个本地项目归属改为 verifier，并用该账号经 HTTP 验证列表和详情均可见。
- 后续按用户要求停下旧 4+5 条项目，并终止其暂停的制作进程以释放索引锁，保留数据和缓存。新 Chrome 窗口创建 `0d60e402ee3140dbba77ed3924078df4`（101689）和 `7f1efce1da0b48d5820a674f9f54d187`（101708），均归属 verifier、各 2 条素材；现均制作完成，状态 review。32 个隔离 Chrome 故障/并发场景通过，未启动真实交付，见[Chrome 验收](verification/hook-publish-dev-chrome-2026-11-10.md)。
- 启动前核对无活动普通/交互/字幕队列；启动后 `/home/healthz` 为 ok、worker_alive=true，登录后两个项目详情均可读取。沿用验收环境的 production 功能上下文以支持纳管流程，但仍是本机隔离数据，非线上服务；FBBM/飞书交付关闭，`media_delivery_enabled=false`。

## 2026-11-04：剪切打码时间轴开发预览

- 本机 `takeover-m2.localhost:18793/home/` 保持原服务与数据，仅将 `feat/mosaic-timeline-live-preview` 的 `index.html`、`mosaic.js` 同步到运行工作树供用户围观；未重启 Python 后端。
- 实现树为 `exact-cut-mosaic-editor`，从 main `9002646` 建立；当时后端 `098ffad` 与此 main 产品代码一致。健康检查 release 为 `development-backoff-098ffad`，前端为本轮开发版本。
- Mac mini 的独立 `mosaic-editor-ui` 工作树用于回归，未更新其现有 dev 服务。
- 本轮范围、测试与实时预览边界见[验收记录](archive/verification/mosaic-timeline-live-preview-2026-11-04.md)。

以下保留两台 Mac 并发补验与暂停/退避修复时的盘点信息。


## 2026-11-06：18794 人工验收实例

- 地址 `http://127.0.0.1:18794/home/`；启动器 `/Users/matao/exactcut-test/start.sh`，repo 链接到本仓库，当前代码 `489c5dd`。
- 用户要求除 FBBM 上车、飞书写入外对齐线上功能。已按生产有效配置同步业务参数；真实 OSS（仅 `exactcut-previews/test/`）、Gemini、OpenAI 兼容 AI、封面剧名提取开启。独立本地数据根与凭据文件保留。
- `delivery_enabled=false`、publish/external-write 开关为 0；本地 guard 继续禁止 FBBM、飞书交付、生产 OSS 前缀及对象删除。SAM 为 CPU，同线上模型文件，仅路径不同。
- 空闲屏障下正常重启，健康正常；目标项目详情 errors=[]。Gemini/OpenAI 小请求成功；图片提取仅完成配置，未重新生成图片验收。完整记录 `/Users/matao/exactcut-test/config/alignment-report-20261106.md`。
- 本地仍是 macOS，不能把工具链和性能等同线上 Linux；当前版本新增的调度及分段重编码配置保留供验收。

## 最近状态

| 机器 / 入口 | 用途与最后依据 | 本次整理时的核对 |
| --- | --- | --- |
| 本机 `8793/home/` | 默认日常开发入口，标准 `bin/dev-web` 使用 | 未发现该端口监听；没有启动或重启 |
| M2 `takeover-m2.localhost:18793/home/` | 并发补验，应用 `098ffad`，最近验收 release 为 `development-backoff-098ffad` | 已只读确认本机 18793 正在监听；本次未重新进行业务验收 |
| mini `takeover-mini.localhost:18792/home/` | 同一产品代码的并发补验，保留原账号、配置和数据 | 沿用 11 月 4 日验收记录，本次未重新连接远端盘点 |
| 本机历史 `18883/home/` | 资源调度、字幕和剪切打码长片验收 | 本次未发现该端口监听；A 长片失败仍保留为待办 |

监听检查只确认服务占用端口，不证明健康、版本一致或没有任务。localhost 指各自机器，不能把 mini 的地址当作本机入口。

两台并发测试实例使用固定 worktree、私有启动器和数据根，合入 main 不会使它们自动加载新后端。应用 `098ffad` 的产品源码与本轮已合并代码一致；后续分支还增加了测试及文档。具体测试边界见[最终验收](archive/handoff/2026-11-04-concurrency-8-final-acceptance.md)。

## 开发约定

- 日常默认入口为 8793；已有服务时先协调，不重复起长期实例。专项实例使用独立端口、代码树、数据及账号，记录负责人和配置。
- 使用已核对的私有启动器，不重跑临时造数脚本来恢复旧实例。不要修改运行中测试树的 Python 源码。
- 不合并模拟发布数据、账号库、media-state 或回执。模拟已入库不代表真实远端交付。
- Cookie 按主机和路径隔离，不按端口隔离；多实例应使用独立浏览器上下文或主机名。

## 操作前检查

1. 读取当前分支、未提交修改、服务用途及其他会话使用情况。
2. 重新核对监听、进程 cwd 和启动方式，不照抄历史 PID，不打印整个进程环境。
3. 检查健康及目标数据根内普通、交互、最终打码、字幕队列和媒体子进程；不能只看网页是否打开。
4. 有运行或排队任务时先协调。仅对确认目标正常停止，等待调度器排空，不批量杀 Python/FFmpeg。
5. 保留数据、媒体、配置和验收证据。停止自己的临时服务后更新本表；未核对的状态明确标注未知。

只读盘点可使用 `lsof -nP -iTCP -sTCP:LISTEN`、`ps -p <PID> -o pid=,command=` 和 `lsof -a -p <PID> -d cwd -Fn`。启动方式见[开发说明](../source/README.md)。

## 本轮保留的边界

并发专项保持 SAM CPU、`preview.namespace=test`、`delivery_enabled=false` 及交付守卫。测试上下文可以是 production，但仍是独立测试实例，不是正式服务。故障注入适配器已在验收结束时退出，OSS 测试媒体为播放检查保留，尚未清理。

最初清单整理仅包含文档与只读监听检查；后续测试实例变更以本文顶部按日期记录的专项条目为准。
