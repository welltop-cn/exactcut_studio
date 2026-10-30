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

## 2026-10-14 粘贴文本统一模型识别（8793 保留运行）

用户要求启动本地dev，并明确授权复用线上真实 `.env` 的AI配置。确认主dev进程cwd、无活动制作/FFmpeg后，重启8793加载提交 `a7d4ce0` 及主工作区已有其他修改。最终PID 67415为历史快照，操作前重新核对。入口 `http://localhost:8793/home/` 已打开，healthz返回development/ok、worker_alive=true。

启动器：`source/.dev-state/clip-text-model-only-20261014/start.sh`，日志同目录 `server.log`；指定Python3.12，沿用主dev数据及preview-concurrency runtime.json。用户授权后，只从 `8.221.122.75:/srv/exactcut/config/production.env` 只读提取OPENAI_API_KEY、OPENAI_BASE_URL及可用模型名到本地私有development.env（0600）；AI=1，素材获取/发布/外部写入仍为0。未修改线上服务。

真实模型通过应用read_clip_text对 `EP4: 00:49:00-02:16:18,, EP5: 00:00:01-01:08:13` 返回正确两段、errors=[]，所有分秒帧逐字段断言通过。此验收为真实模型+应用函数，非鉴权HTTP接口验收。最初验证进程因继承OPENAI环境变量发生401；load_env_file使用setdefault，清除继承变量后通过。不能由该401推断配置文件密钥无效；dev启动器已清除父进程OPENAI变量。

## 2026-10-14 剪切替换本地验收（8800 保留运行）

用户要求打开本地 dev 观察。8800 健康检查为 `development-cut-replace`，使用主工作区未提交代码；私有启动器 `source/.dev-state/cut-replace-20261014/start.py`，日志同目录 `server.log`。指定 Python 3.12、worker 关闭，沿用开发数据，禁止真实外部发布和写入。8793 未改动。

验收项目：`http://localhost:8800/home/#2cf132715d8e47719f5c275f0f6f2d13`，原 101606 / 150 秒项目的独立复制件。真实 FFmpeg 删除两段共 60 帧，产出 4440 帧 / 148 秒；浏览器已验证播放、取消替换不改变当前成片、确认替换后再次编辑使用 4440 帧新成片。原项目及原文件保留。页面已交还用户，观察期间不要自动刷新或重启服务。

服务启动后的后台边界修正尚需后续重启加载；静态界面读工作区当前文件。线上 OSS/FBBM 未在该验收环境实际写入，相关上传选择通过隔离测试验证，不能把本地验收当作真实交付成功。

2026-10-14 更新：等待用户本次剪切任务 ready 后，取得已有 cut-locks 排他锁，再从已核验 PID 74360 正常重启 8800 为 PID 74366（历史快照）。加载 managed 交付路径修复及 force 重新识别；原150秒测试项目本次生成结果3682帧已保留。8793未动。

2026-10-14 再次更新：无活动剪切/识别任务、取得排他锁后，将8800从已核验PID74366重启至84455（历史快照，操作前重新核验），加载放弃剪切的后端重置接口。healthz确认development-cut-replace/ok。用户候选保留，8793未动。

## 2026-10-16 打码效果设置（8801）

独立 mosaic-gemini 工作树提交 `63ca7a5` 增加样式/强度/范围扩大。确认 8801 原 PID 95652 的 cwd，取得 compute/scan 排他锁后正常重启，当前 PID 78076（历史记录，操作前仍须核验）。原启动器 source/.dev-state/mosaic-gemini-20261016/start.py、隔离开发数据与配置沿用，release 为 development-effects-63ca7a5；8793/8800 未动。54 项相关效果/交互/渲染/跟踪测试通过，真实 FFmpeg 覆盖三种效果及短片帧数。浏览器确认默认收起、展开三项设置、原删除3段/打码4段草稿保留。未重新生成或替换用户视频。

## 2026-10-16 生成锁定与停止（8801）

提交 `0f09503` 增加生成期间编辑锁与保留草稿的停止生成接口。67 项相关测试通过，另复查精简后的运行中按钮；测试包含真实子进程终止、资源释放、同方案重试、停止确认前禁止编辑。浏览器实看当前任务运行时选择/取消/效果/区域编辑禁用，播放与查看可用。

等待用户任务 render-b63091de29500d5e2d16a26dd2d79a2f 自然 completed，取得 compute/scan 锁后从已验证 PID 78076 重启为 PID 14916（历史记录，操作前核验）。release development-stop-0f09503，healthz ok/development；用户本次结果完整保留，未替换成片或变更选择。8793/8800 未动。
## 2026-10-16 马赛克 Gemini 独立验收（8801 保留运行）

用户要求启动本地 dev 测试。专用入口 `http://localhost:8801/home/#2cf132715d8e47719f5c275f0f6f2d13`，owner 为 mosaic-gemini 会话。PID 52609（操作前重新核验），cwd `/Users/matao/.config/superpowers/worktrees/exact-cut-fbbm-handoff/mosaic-gemini/source`，提交 `0b2748b`。healthz 已确认 development/ok、release `development-mosaic-0b2748b`。

私有启动器 `source/.dev-state/mosaic-gemini-20261016/start.py`，日志同目录 `server.log`。复用 8800 的开发配置 `development-cutting.json`、本地 Gemini 配置路径和开发数据 `source/.dev-state/data`，Python 使用指定 py3.12；worker、发布、外部写入关闭。8793/8800 未重启。此入口共享开发数据，避免在多个入口同时编辑同一成片。浏览器已打开，留给用户验收；本次只验证服务健康，未发起真实 Gemini 扫描。验收完成后可停止此会话的专用实例。

2026-10-16 渐进识别更新：等待旧扫描 completed（8 个区间）后，取得 scan/compute/batch 排他锁，8801 从 PID 52609 重启到 37538，release development-mosaic-3bb3f06。原结果保留，新识别跳过剪切精细复查、先展示时间段再逐段发布区域。healthz 已确认 ok。

2026-10-16 SDK schema 修复：取得扫描/计算/项目排他锁后，8801 从 PID 37538 重启至 63248，release development-mosaic-b38f802，healthz ok。已备份并校验恢复该项目 7 个区间（6 个已定位）；未重新调用模型。

## 2026-10-19 片段独立打码效果（8801）

加载 37f74c4（含 5b9d306 替换轮询竞态修复、2048376 柔边基础）。63 项效果、真实 FFmpeg、跟踪和浏览器测试通过。每片段独立样式/强度/扩大；pixel、blur（旧硬边）、soft_blur、solid。旧草稿默认兼容，预览与批量生成传递一致。

确认无 running 记录，取得 compute/scan 排他锁，核对 PID 14916 的端口/cwd 后正常重启为 PID 7447（历史快照，操作前重查）。沿用隔离启动器及配置，healthz=ok/development，release=development-clip-effects-37f74c4。原选择和结果保留，未调用模型或替换用户成片，8793/8800 未动。

## 2026-10-19 效果弹框生成全部区域片段预览（8801）

加载 a59b213。65 项相关测试通过，真实 FFmpeg 验证短片多区域与原成片保留；UI 支持未保存效果试预览、返回停止。确认无运行任务、持有 compute/scan 排他锁，核验 PID 7447 cwd/端口后重启为 PID 23345（历史快照）。healthz ok/development，release development-clip-preview-a59b213；独立配置沿用，其他端口未动。未改用户草稿或结果，未触发真实模型调用。

## 2026-10-19 片段预览加速（8801）

加载 9906e54：复用原始跟踪遮罩，批量跟踪跳过单目标视频编码，片段预览长边640，正式输出分辨率不变。现有10秒片段的已完成遮罩只读验收，新版裁剪合成10.09秒；不含首次推理。详见 verification/clip-preview-performance-2026-10-19.md。

确认无running任务，取得compute/scan排他锁，核验23345监听与cwd后正常重启为52843（历史快照，后续重查）。healthz ok/development，release development-fast-preview-9906e54。沿用隔离配置与数据，原视频、草稿和结果保留，未触发真实模型或替换成片。

## 2026-10-19 固定位置字幕打码（8801）

加载 `355c83e`，手动添加默认固定位置，已有 AI/旧草稿继续跟踪；完整输出和片段预览共享固定矩形遮罩，无需 SAM。渲染/跟踪/效果 32 项、完整 UI 54 项通过，最终默认模式补查 10 项通过；真实 FFmpeg 覆盖遮挡起止帧及片段预览像素。

核验 PID 52843 的监听/cwd，持有 compute/scan 排他锁且无 running 马赛克任务后正常重启为 PID 21306（历史快照）。沿用原隔离启动器/配置/数据，release `development-fixed-position-355c83e`，healthz ok/development。用户草稿、原视频和现有成片未修改；8793/8800 未动。

## 2026-10-19 审查修复验收（8801）

用户授权修复并发布本地dev。完整1546项回归通过后，在持有compute/scan锁且无活动任务时，核验原PID21306端口/cwd并重启为15969（历史快照）。release development-review-fixes-17972d9，health ok。修复多区域motion污染、取消打码后改时间意外启用、放弃合成不取消进程；账号、草稿、视频保留。8793/8800未动。另按后续授权部署线上测试，见verification/mosaic-test-deployment-2026-10-19.md。

## 2026-10-26 封面剧名自动提取验收

- 8793 已在核对无 queued/running 任务后重启，PID 84274，工作目录为本仓库 `source`，Python 为用户指定的 py3.12。
- 启动：`./bin/dev-web --title-images`；配置：忽略目录 `.dev-state/title-images/runtime.json`，通过集中配置启用独立的剧名封面查询/图像编辑能力。专用凭据保存在同目录权限 0600 的文件中，未写入版本管理。
- 通用 `EXACTCUT_ALLOW_AI`、`EXACTCUT_ALLOW_MATERIAL_FETCH`、外部写入和发布权限仍为 0。健康检查为 OK，worker 存活。已有用户项目未重跑。
- 剧名无需单独预览确认；勾选后随制作自动提取，按剧缓存。101607 的真实 API 图片已缓存供本地复用。
- 验证范围和隔离测试视频见 [记录](verification/title-extraction-2026-10-26.md)。生产未部署。

### 2026-10-26 正片下载接通

用户授权后，将 8793 重启为 PID 96508（操作前已确认无活动任务）。启动参数为 `--title-images --fetch-env <source>/.dev-state/runs/real-eight-20261010/development.env`。使用已有开发验收凭据、`exactcut.dev_fetch` 只读适配器和 `EXACTCUT_MATERIAL_FETCH_CMD` 扩展点，material_fetch=1；上传、发布仍禁用。该适配器只获取已存在的 OSS 对象，缺失不触发远端复制。项目 49a94fe1f5ce49c7adf4bc47e5fc4c10 已重试，101607/EP1 下载完成 219439097 字节并进入制作。下载相关 15 项测试通过。

### 2026-10-26 封面查询 TLS 连接修复

8793 在确认无活动任务后重启为 PID 23280，继续使用 title-images + fetch-env 配置。本机 fake-IP 路径出现 KES TLS EOF；独立复现后，局部 `title_extraction.kes_connect_ip=47.117.169.192` 保留原域名 Host/TLS SNI，真实 101606 封面查询成功。项目 cbe91a69163a4ffab66691016b2604b5 已重新提交，生成透明图缓存并进入 FFmpeg 渲染。相关16项测试通过。封面网络错误最多尝试3次，不自动重试已发送的图像编辑请求。

2026-10-26 字幕图层调整：按用户要求，字幕位置不动，叠在剧名上层。预览、普通 FFmpeg 和片段特效路径一致；新增真实像素测试通过。确认无活动任务后重启8793至PID 70278，health正常，未重跑用户成片。带水印输出与预览缓存版本更新为2。

2026-10-26 Pi审查修复：缓存/pin/直接引用损坏自愈、按剧锁、logo包数据已修；28项相关测试及隔离wheel检查通过，Pi+DeepSeek V4.1 Flash复查确认。确认无活动任务后重启8793至PID 95498，health正常，沿用原参数/凭据，未重跑用户成片。

2026-10-26 原片下载：确认无活动任务、无 ffmpeg 后，将 8793 从 PID 95498 重启为 PID 71732（操作前须再核验）。沿用原 EXACTCUT 参数：development、material_fetch=1，发布、AI、外部写入仍为 0。加载原片列表与浏览器临时签名下载。未重跑用户成片。日志 `source/.dev-state/originals-ui-20261026.log`。

2026-10-26 原片保存目录：确认无活动任务后，将 8793 从 PID 71732 重启为 PID 23012。沿用原参数。Chrome 下载可选文件夹，文件写入该文件夹下的剧号目录。未重跑用户成片。

2026-10-26 原片未保存剧号：确认无活动任务、无 ffmpeg 后，将 8793 从 PID 23012 重启为 PID 3926（操作前须再核验）。沿用 development、material_fetch=1，发布、AI、外部写入为 0，正片下载命令仍是 `python3.12 -m exactcut.dev_fetch`。列表和单集地址使用输入框里的剧号，不必先点保存。未重跑用户成片。

2026-10-26 水印参考图：确认无活动任务、无子进程后，将 8793 从 PID 3926 重启为 PID 85020（操作前须再核验）。沿用 development、material_fetch=1，发布、AI、外部写入为 0。参考图改为渐进 JPEG。未重跑用户成片。8794–8801 未动。

## 2026-10-30 Gemini 双语字幕实验（8810 保留运行）

- 入口 http://localhost:8810/ （监听127.0.0.1:8810），owner：preview-subtitle-demo / 主程GPT；用户要求留给观看。PID 41347 为本次快照，操作前重新确认。
- cwd `/Users/matao/.config/superpowers/worktrees/exact-cut-fbbm-handoff/preview-subtitle-demo/source`，分支 `experiment/preview-subtitle-demo`。启动：指定Python `-m exactcut.subtitle_demo --ai-env /Users/matao/code/exact-cut-fbbm-handoff/source/.env --port 8810`；使用 `config/development-cutting.json` 集中配置。
- 私有数据/缓存/日志：上述cwd的 `.dev-state/subtitle-demo/`，项目配置副本为其 `project/`，独立限流状态在其 `runtime/`。`server.log`/`server.pid` 记录运行信息。
- 用户授权只读下载指定batch 91f0aaa0aae54aceb28093ea8206b7d2 当前版本成片及项目配置；当前version c7a295a6b8f54689a1f9f1a775218011，完整60.1秒视频SHA f2d7b2ce954b87313d661087bd879e43f45f195ce62a0e7db1e885a7ea84d683。未复制全局生产凭据/账号或激活生产任务。
- Gemini实际整片识别38.762秒/21条；5次seek、字幕间隙及播放同步验证，7项解析检查通过。完整抽查记录在该worktree的 `docs/verification/preview-subtitle-demo-2026-10-30.md`。8793/8800/8801未变更。

## 2026-10-30 中文字幕预览集成（8821）

独立 worktree `preview-subtitles` / `feat/preview-subtitles`，入口 `http://subtitle-preview.localhost:8821/home/#91f0aaa0aae54aceb28093ea8206b7d2`。用户授权指定素材和配置复制件；独立账号及 registry，正式 worker/发布/外部写入关闭。启动器为 worktree 的 `source/.dev-state/preview-subtitles/start.py`，数据同目录 `data`。核验旧 PID 57727 的 cwd 且无活动任务后正常重启为 PID 32645（历史快照）；仅加载本地未提交功能。真实识别38.375秒/21句，原生成下载SHA一致；队列及UI等46项相关测试通过。保留8821供用户观看；8793/8800/8801/8810/8820未动。详细记录位于该 worktree `docs/verification/preview-subtitles-integration-2026-10-30.md`。

用户追加授权复制项目 `0d6fdddb468348ad92e0cbd8f5de1db5`，供本人测试多个素材生成中文字幕。已在同一 8821 隔离数据根登记，入口 `http://subtitle-preview.localhost:8821/home/#0d6fdddb468348ad92e0cbd8f5de1db5`，名称「中文字幕多素材测试 · 101682（线上副本）」。复制 4 条当前版本原片及轻量预览，共 744300069 字节，8 个视频 SHA-256 均与线上版本记录一致；配置原件和来源清单保存在该 worktree `source/.dev-state/preview-subtitles/imports/0d6fdddb468348ad92e0cbd8f5de1db5/`。本地媒体路径已重映射，使用独立 registry 和既有 subtitle_demo 账号；未复制生产账号或激活制作/上传任务。已验证项目 HTTP 200、全部视频 Range 请求 206、4 条字幕均 idle；未提交字幕任务、未调用 Gemini，留给用户测试。8821 无需重启，其他服务未动。

随后按用户要求修复识别失败并验收两条素材并发：8821 在确认队列为空后重启加载修复，最终 PID 61192（操作前重新核验），数据、账号和入口保留。默认两条素材同时识别、其余 FIFO 排队；使用哈希验证过的轻量预览、不放大、4fps，超预算自动拆段，失败段一次校验重试、成功段持久缓存。最终四条 completed，99/113/103/110 句；第三、第四条真实并发整组108.314秒，队列已空。56项相关测试和浏览器单轨/缓存/跳转通过，8个原片/预览哈希不变。48点人工抽帧中有1处短句结束偏晚，另有1处翻译漏时间限定，准确性仍有明确未解决项；不能据生成成功声称逐句逐帧准确。详见 preview-subtitles worktree 的 `docs/verification/preview-subtitles-four-materials-2026-10-30.md`。未上线。

## 2026-10-30 素材校验红色高亮验收（8793 保留运行）

用户要求启动并打开本地 dev。核验 8793 原 PID 85020 的 cwd、无活动任务及子进程后，沿用原开发数据、title-images / dev-fetch 配置重启，当前 PID 62449（后续操作须重查），release `development-material-validation-aebc64a`。补充 `/opt/homebrew/bin` 至启动 PATH，以便本地 ffmpeg/ffprobe 可执行。healthz 为 development/ok、worker_alive=true；其他端口未动。

测试项目 `42ee83307dad4932add4d05db69b7251`，入口 `http://localhost:8793/home/#42ee83307dad4932add4d05db69b7251`。独立合成剧号 999981、EP4 105 秒测试图，六条素材编号 1/2/3/4/5/8；第六条故意留空集号、开始04:00:000晚于结束01:42:016。将素材8集号改为4、开始分钟改为00即可通过后端校验，已有原片帧索引。项目已纳管并分配独立本地账号 validation_1030；私有登录信息与日志在 `source/.dev-state/material-validation-20261030/`，不提交凭据。

真实 HTTP/Playwright 验证登录、六条素材、正确卡片/字段高亮、保存后聚焦、点击顶部提示定位、逐项纠正清除红色提示及刷新后保留初始错误。发现纳管保存期间输入框禁用导致过早聚焦失败，已改为操作恢复编辑状态后再聚焦；旧版/纳管两种回归及相关测试共18项通过。测试项目最终仍保留两处初始错误，未启动制作。已按用户要求用系统浏览器打开入口，服务保留供验收。

用户要求扩大测试后，最终完整回归为1770 passed、1 skipped（既有 mosaic-only 比较条件），无失败；专项77项通过。补充修复特殊字符集号、空帧字段误判时间先后，以及复制纳管项目时持久化临时校验投影的问题，提交 `db4c684`。确认无活动任务/子进程后，8793 从已核验 PID 62449 重启为 PID 25659（操作前仍须重新核验），沿用原配置；healthz=ok/development、worker_alive=true，release `development-material-validation-db4c684`。重启前后测试项目 media-state.json SHA-256 一致；HTTP复核仍为6条素材、素材8的两处初始错误。用户编辑数据未重置，其他服务未动。
