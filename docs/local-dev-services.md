# 本地开发与专项服务清单

整理日期：2026-11-04。这里保留默认开发约定和最近专项实例；旧端口、PID、启动脚本流水账见[历史清单](archive/local-dev-services-before-cleanup-2026-11-04.md)。

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

本次仅整理文档与只读检查本机监听，未操作上述服务或数据。
