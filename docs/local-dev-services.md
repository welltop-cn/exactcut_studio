# 本地 dev 服务与专项测试交接

最后盘点：2026-10-13（Asia/Shanghai）。本记录区分已确认现状与后续约定，端口/PID/版本在操作前须重新检查。

## 已确定的使用方式

- 日常开发统一使用主工作区 `main`，入口 `http://127.0.0.1:8793/home/`。
- 不需要把专项模拟数据合并到主 dev；保持项目、回执、测试脚本、成片和证据各自独立。不要把模拟“已入库”当作真实平台交付。
- 专项测试可按需临时启动，标明用途、端口、代码工作区、数据根和模拟边界；结束后关闭自己启动的服务，保留必要证据。
- 2026-10-13 本轮仅盘点、编写文档：**8793–8797 五个服务仍在运行，尚未执行停服、重启或数据迁移。** “统一保留8793”是后续收敛目标，不能写成已经完成。

## 服务清单（盘点时）

| 端口 / PID | 用途 | 代码及启动入口 | 数据及后续处理 |
|---|---|---|---|
| 8793 / 68925 | 主开发服务，真实 worker | 主工作区 `source`，`python -m exactcut.webserver`；本次启动脚本 `source/.dev-state/preview-concurrency-20261012/start.sh` | `source/.dev-state/data`；长期保留入口 |
| 8794 / 72099 | 旧 review_demo 演示 | `python -m devtools.review_demo.server --ai-env .env`；进程 cwd 为主工作区 `source` | 当前 checkout 已无该模块文件；根路径请求断开，用途仅从进程命令确认；不作为有效验收服务 |
| 8795 / 14491 | 飞书临时查询失败专项模拟 | `feishu-read-retry/source` worktree；`/tmp/exactcut-feishu-dev.py` | `/tmp/exactcut-feishu-dev-final-20261012`；按需复测，不并入主 dev |
| 8796 / 55419 | 整合版本飞书故障恢复模拟 | `release-all-20261012/source` worktree；`/tmp/exactcut-all-dev.py` | `/tmp/exactcut-all-dev-20261012`；按需复测，不并入主 dev |
| 8797 / 66134 | 整合版本真实视频制作验收 | `release-all-20261012/source` worktree；`/tmp/exactcut-all-real-dev.py` | `/tmp/exactcut-all-real-dev-20261012`；保留测试成片及证据 |

worktree 根为 `/Users/matao/.config/superpowers/worktrees/exact-cut-fbbm-handoff/`。主工作区为 `/Users/matao/code/exact-cut-fbbm-handoff`。上述 `/tmp` 和忽略目录为本机私有临时文件，不随 Git 分发，也不保证永久存在；复用前核对脚本，不能当作受版本管理的标准启动器。

## 真实处理与模拟边界

### 8793 主 dev

- 独立开发数据、账号和配置；正式 worker 开启。盘点时健康正常，未发现 queued/running/producing 等活动任务，也没有 FFmpeg 制作进程。
- 启动脚本禁用真实外部写入、发布、AI 和素材获取；不是生产凭据/真实交付环境。
- 现有实例使用私有 `preview-concurrency-20261012/runtime.json`，`render=3`、`hook_previews=3`。标准 `source/bin/dev-web` 则读取 `source/config/default.json`，两者不能直接视为同配置。重启前确认是否需要保留本次并发设置，沿用项目配置机制。
- 进程在2026-10-12 18:46启动；主工作区后来合入/更新代码不意味着已经加载最新后端。Python后端不会因Git更新自动重载，静态前端可能已读取新文件，须防止新前端配旧后端。

### 8795 / 8796 模拟服务

- 正式 worker 关闭；专门测试循环触发实际查询恢复业务逻辑。
- Feishu GET 被替换为模拟响应：先 `1255001 / InternalError`，再返回测试记录；FBBM状态和视频ID同样是模拟值。
- 上传及外部写入被禁用/拦截；脚本为了测试界面另将 `delivery_enabled` 替换为真，不能据此理解为可正式上传。
- 8796 项目 `5d8e3f3a98794ff382dfc507ec82929b` 是生成的测试夹具，不是线上项目副本。验收结果：19次模拟GET、7轮poll、外部写入0，最终fbbm_ready。它证明恢复逻辑与界面状态正确，不证明真实飞书/Facebook交付成功。
- `remote-ready`、`run-poll`、`auto-poll` 等文件控制模拟场景；不应把这些控制机制接入日常主 dev。

### 8797 真实渲染服务

- 正式 worker、FFmpeg和质检实际运行，素材为本地开发样本的复制件；外部发布、写入、AI、素材获取关闭。
- 项目 `c59c0a15026c42d3903b779252c77287` 产出11秒定格片尾版和12秒图片片尾版，均qc_passed，文案画面及片尾静音已验证。证据为数据根下 `real-acceptance.json`。
- 盘点时没有活动任务。与8796用途不同，不能将8796的模拟结果当作这里的真实渲染证据。

## 其他 agent / session 操作前检查

1. 先读本文件，再核对 `git status`、当前分支、监听端口和进程cwd。不要因端口空闲就另起长期dev，也不要停止其他项目的Python服务。
2. 只读盘点命令：`lsof -nP -iTCP -sTCP:LISTEN`、`ps -p <PID> -o pid=,command=`、`lsof -a -p <PID> -d cwd -Fn`。不要打印完整进程环境或凭据。
3. 8793/8797健康接口为 `/home/healthz`，8795/8796为 `/healthz`。健康ok仅说明服务响应，不证明代码最新或交付真实。
4. 停服/重启前检查该数据根 `.web-state/jobs/*.json`、`batches/*/runtime.json` 的活动状态，并核实是否有其他session正在使用。PID仅是历史快照，不能照抄直接kill；重新确认后只对目标进程正常终止。
5. 保留项目数据和测试证据；不要合并accounts、media-state、publication或伪造回执，不为清理服务删除数据目录。不要重跑会重新造项目或写配置的临时脚本来“恢复原实例”。
6. 经协调收敛后更新本表，明确哪些服务已停、主dev何时从哪个提交重启、实际配置以及健康/任务检查结果。

标准开发启动方式见 [source/README.md](../source/README.md)，使用指定Python `/Users/matao/miniconda3/envs/py3.12/bin/python3.12`。已有8793监听时不得直接启动第二实例。

浏览器Cookie按主机/路径隔离，**不按端口隔离**。多个127.0.0.1服务登录可能覆盖Cookie或造成401；临时测试应使用独立浏览器上下文或明确的主机隔离，不能据此误判业务失败。

## 关联证据

- [整合发布验收](verification/all-sessions-release-2026-10-12.md)
- [飞书查询恢复验收](verification/feishu-read-recovery-2026-10-12.md)
- [字幕预览并发验收](verification/dev-preview-concurrency-2026-10-12.md)

线上版本会继续变更，以发布管理器和公网健康接口为准；本地旧worktree/旧进程不会随生产发布自动更新。

## 2026-10-13 Hook 20 秒临时验收（已停止）

本次 Hook 时长调整会话使用 `127.0.0.1:18893`、独立数据根 `/tmp/exactcut-hook20-dev-20261013`，真实 worker/FFmpeg 完成三条制作并通过 QC。所有临时任务完成后，核对 PID 24013 对应 `/tmp/exactcut-hook20-dev.py` 再正常停止，并确认端口无监听。数据保留；未重启共享 8793 或操作其他会话实例。详见 [验收记录](verification/hook-duration-20s-2026-10-13.md)。

## 2026-10-13 段首字幕与闪白验收（8793 保留运行）

用户要求开发并打开本地 dev 验收。检查原 8793 进程 cwd、活动任务和 FFmpeg 后，正常停止 PID 68925，并用原启动脚本 `.dev-state/preview-concurrency-20261012/start.sh` 与指定 Python 重启。新 PID 15177 为本次历史快照，加载主工作区字幕与闪白未提交改动；后续操作仍须重新确认 PID。沿用隔离开发数据，外部写入、发布、AI 和素材获取关闭，render/hook_previews 并发均为 3。

健康接口返回 development/ok，worker 正常；三条真实成片均 qc_passed。浏览器验收入口使用 `localhost:8793`，项目 `4d12a4cb10f748f6a45840e5b0ea3ee2`，保留服务供用户验收。日志：`source/.dev-state/segment-effects-dev-20261013.log`。详见[验收记录](verification/segment-effects-dev-2026-10-13.md)。

字号/行数反馈更新：8793 在确认无活动任务后从 PID 15177 正常重启为 PID 60490（历史快照），沿用上述隔离配置。新验收项目 `3523c9fb2443490381217c2d69c1bb8f` 已完成三条真实制作，均 qc_passed；72 px、最多三行，原项目保留作对照。

闪白时长反馈更新：确认无活动任务后，8793 从 PID 60490 正常重启为 PID 69384（历史快照），配置不变。项目 `292f4f2ab93a44ae8cb4a080e5a006a2` 提供硬切、默认 0.5 秒、自定义 0.8 秒三条成片，均 qc_passed，保留运行供用户验收。

闪白上限更新：确认无活动任务后，从 PID 69384 正常重启为 PID 78630（历史快照），沿用隔离配置。前后端均支持 0.2～5 秒，默认 0.5 秒；healthz 返回 development/ok。
## Source frame picker temporary preview (2026-10-13)

- `http://localhost:8799/home/#95f99b49174e4cbb80dbbe381abde05c`: new explicit frame-format interaction demo; original millisecond copy `bb380fdda28544e98ed8839a2dfa1972` remains unchanged.
- Owner: source-frame-picker feature session, worktree `/Users/matao/.config/superpowers/worktrees/exact-cut-fbbm-handoff/source-frame-picker`. Private launcher `/tmp/exactcut-source-picker-dev-20261013/start.py`, data sibling `data`; recheck listener and command before any service action.
- Worker, source fetching, AI, external writes and publishing disabled. The user explicitly requested readonly copies of the production project's configuration and video for local testing. No production mutation; no shared 8793 restart.
- User requested a live preview, so leave this dedicated instance available until inspection is complete. Do not remove its data or treat it as production. Code is uncommitted and not released.

## 服务器独立测试实例部署文档

服务器测试环境使用固定 IP 入口 `/test/`，与本机 dev 不同。其他 agent/session 更新前请阅读 [服务器测试部署手册](server-test-deployment.md)，复用既有容器、数据、账号及资源限制；测试通过不自动发布正式服务。
