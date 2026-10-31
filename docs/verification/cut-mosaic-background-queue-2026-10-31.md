# 剪切打码后台队列：D1 实现与本地验证记录

日期：2026-10-31。分支：`feat/cut-mosaic-background-queue`。基线：`5b0e4081b7c894f1f5b03213e100dd5de5347852`。

状态：D1 实现与本地验证完成，等待父会话独立 review；本记录不代表用户最终验收，也不授权上线。设计契约见 [剪切打码后台队列设计](../superpowers/specs/剪切打码后台队列设计.md)。没有部署、合并 main 或调用外部发布接口。

## 实现边界与文件分工

- `exactcut/mosaic_queue.py`：独立 `.web-state/mosaic-queue.json`、schema version 1；持久提交快照、按用户 FIFO / 用户轮转、领取槽位、有限恢复与跨进程 dispatcher lease。没有读写字幕的 `queue.json`。
- `mosaic_render.py`：最终生成只做校验和持久入队；冻结 source SHA / reset / targets / ranges / effect / tracking config / authenticated actor。worker 领取原有 `compute.lock` 后复查账户启用、项目权限、源版本和源快照哈希，再调用原有真实媒体流水线。片段预览继续受原有 compute 门控。
- `cutting_access.py`、`material_versions.py`、`batch_control.py`、`workflow.py`：复用原有跨进程素材 gate，保护旧剪切入口、同素材写入、重新制作/交付入口、项目归档/删除及冲突修改。纳管草稿保存逐一比较冻结素材和项目公共参数，允许其他素材独立修改。`can_edit_video` 保留进入编辑器的能力，实际修改权限仍在服务端判断。
- `mosaic_session.py`：原片准备使用单独 `mosaic-prepare.lock`，不等待长任务持有的 compute 锁；抽帧仍受 frame lease 约束。同一素材的只读重开、播放、帧读取和最终任务查询可用。
- `mosaic_replace.py`、`cutting_replace.py`：显式内部 job 身份通过冻结保护，复用既有 prepare/commit、哈希/版本与替换恢复协议；不自动替换。`discard-result` 只放弃结果，保留源片和提交参数。
- `processing_cancel.py`、`processing_child.py`：协作取消、真实子进程组终止；worker/compute/batch leases 传给进程守护及实际子进程。守护核验独立进程组，监视父服务器是否仍是原父进程，父服务器消失后杀死整个所属进程组。恢复器不会在旧子进程仍持租约时抢跑。
- `mosaic_http.py`：身份取自 `handler.user['username']`，不采信请求中的 actor；状态/预览与写操作分流，新增明确的放弃结果入口。
- `web/mosaic.js`：先查服务端任务再显示可编辑工作区；恢复已提交参数；queued/running/cancelling/待确认均冻结；任务完成自动进入预览与确认。轮询错误保留锁定并重试；旧响应继续受 epoch/token 约束。退出不取消最终任务。取消/放弃后的本机后续调整不会被旧提交快照覆盖。素材列表没有新增进度。
- `webserver.py`、`deploy/linux_ops.py`：dispatcher 纳入启动失败清理、SIGTERM/SIGINT 正常排空和发布维护检查。维护期间不领取新任务；pending 可以跨代码切换保留，active 或损坏的队列阻止停止服务。

## 持久权威、锁顺序与恢复

队列文件保存 `sequence`、`actor_turns`、descriptor 列表（task ID、batch/session/request、actor、order、pending/active/done、完整提交 job、recoveries）。每个会话的 `render-<id>.json` 是执行进度/结果/取消意图/确认决定的权威，`render-latest.json` 是可修复索引。`queued_task` 区分新增持久任务与历史会话；历史完成结果不进入新队列重跑。

提交先原子保存队列，再写报告和 latest。报告写入失败时，队列仍有完整快照，dispatcher 会修复投影。完成/失败/取消报告先落盘，之后释放队列槽位。latest 带提交顺序保护，旧任务恢复不能覆盖更新任务索引。重复请求在素材 gate 内比较标准化参数指纹；活跃或待确认的同方案返回同一个 ID，不同方案拒绝。取消/失败/放弃后同方案生成新的尝试 ID。

实际锁路径如下：

1. HTTP 外层 batch shared lease → mutation barrier → batch shared / asset exclusive edit gate → 纳管 media lock（如适用）→ mosaic queue lock → render state lock。队列写入不做媒体处理、不等待 compute。
2. dispatcher 持有独立 `mosaic-dispatcher.lock`，覆盖它启动的全部 worker。领取时 mutation barrier → queue lock。恢复时 queue lock → 短报告锁；worker lease 仅非阻塞探测，不抢占活进程。
3. worker lease → 非阻塞重试 compute lease → 短素材 gate / batch shared lease / 报告更新，然后退出素材 gate 做媒体处理。完成时短素材 gate → render state lock；重处理期间不长期持有素材 gate。worker 不在持有报告锁时调用 queue 写入。
4. 确认替换持素材 gate，用明确 `allow_job` 通过自身冻结；沿用既有纳管提交锁与原片版本复查。只读状态不以自身任务冻结为失败理由。

执行任务失去 worker lease 后最多从头恢复 2 次；第三次中断标为失败并释放后续任务。取消意图优先，不恢复已取消任务。报告已完成但槽位未释放的任务只释放槽位，不重复处理。关闭时停止领取；尚未开始的计算等待可退回 pending，实际运行任务排空后再释放 dispatcher lease。异常终止时租约与进程守护覆盖旧媒体子进程。

全站领取上限通过集中配置 `mosaic_queue.max_active_tasks` 控制，默认 1；按 actor 限制每用户至多一个 active，用户的多个项目共用该 actor 队列。原有共享 `compute.lock` 仍保留，当前实际重计算也维持串行。没有把“调大领取槽位”当作已经验证的并行吞吐能力；提高真实重计算并行度需要单独资源门与峰值容量验证。

## 自动化验证

固定解释器：`/Users/matao/miniconda3/envs/py3.12/bin/python3.12`。全部测试使用独立临时数据；真实 FFmpeg 源为合成帧号条纹 / 音频脉冲或纯色视频，没有使用生产数据或生产凭据。

最终完整回归：**1921 passed / 1 skipped / 10 warnings，0 失败，475.11 秒**。10 个警告均为既有 multiprocessing 在多线程测试进程中调用 fork 的 DeprecationWarning（delivery observer 4、managed scheduler verification 3、material versions 3），没有隐藏失败。完整回归之后又执行了同 actor 跨项目场景加强后的 queue core：8 passed。

```sh
cd source
/Users/matao/miniconda3/envs/py3.12/bin/python3.12 -m pytest -o addopts='' -q
```

已完成的集中验证：

- 提交候选最终队列专项：29 passed / 24.68 秒，包含两个真实认证账号、跨项目每用户串行、真实 HTTP+Chromium+FFmpeg、服务生命周期与真实子进程组退出。
- 143 passed / 1 skipped：队列、真实 HTTP、真实媒体、界面、OSS 播放与只读用量回归。
- 27 passed：持久队列、两个真实认证账号、公平调度、真实 HTTP+Chromium、维护、投影故障、子进程租约。
- 21 passed：启动失败清理、既有启动恢复、只读用量、真实子进程退出；完整回归曾发现启动异常泄漏 dispatcher 导致后续测试目录被后台写入，已先用红灯重现，再修复生命周期，未靠放宽只读用量断言规避。
- 52 passed：最终守护/真实 FFmpeg/渲染/HTTP+Chromium/发布维护/只读用量专项。
- `node --check exactcut/web/mosaic.js`、目标 Python 模块 compileall、`git diff --check` 通过。
- 本地离线 wheel 构建通过，包含 `mosaic_queue.py`、`processing_child.py`、`processing_cancel.py` 与 `web/mosaic.js`；没有 `.dev-state` 或 `.env` 文件。

唯一既有 skipped：`test_editor_oss_playback.py` 的 cut 参数分支不适用 mosaic 前后比较，原因 `mosaic comparison only`。完整测试产生的 multiprocessing/fork 警告单独记录在最终结果，不把警告当失败或隐去。

## 验收矩阵与实际断言

| 设计范围 | 实际用例 / 结果 | 证据位置 |
|---|---|---|
| 仅剪切、固定打码、混合 | 真实 FFmpeg 验证整数/分数帧率、源帧映射、头部/中间删除后遮挡边界、实际像素、输出帧数/分辨率、音频保留、未确认源 SHA 不变 | `test_mosaic_render.py`；公开媒体结果 JSON |
| 跟踪打码 | 使用明确的确定性跟踪结果/遮罩替代 SAM 推理，实际执行组合、跳帧/失跟告警、多个区域与效果的 FFmpeg 输出；没有声称验证真实 SAM 模型质量 | `test_mosaic_render.py`、`test_mosaic_tracking.py` |
| A 忙时 B/C 提交 | compute 被占用时真实 HTTP 打开 B、抽帧、提交 A/B；两个认证用户连续提交四条实际剪切任务，全部完成且峰值执行数为 1 | `test_mosaic_queue_integration.py` |
| 每用户 FIFO / 公平 | 认证用户 queue_alice 与 admin 的实际处理顺序为 Alice-1 → admin-1 → Alice-2 → admin-2；三用户持久队列轮转无饥饿；同 actor 跨项目仍不并行 | integration 与 `test_mosaic_queue.py` |
| queued 生命周期 | Chromium 创建 A/B；关闭 A 编辑器后 B 可提交；重开 A 恢复六帧删除参数；取消 A 后解锁；整个浏览器退出时 B 仍 queued | `test_mosaic_queue_browser_http.py`；18883 浏览器公开证据 |
| running/cancelling | 实际 owned 子进程中止，cancel_requested 持久，停止确认前保持冻结，终止后释放 compute，保留参数、可相同方案重提 | `test_mosaic_render.py` 的 stop 用例；queue UI 用例 |
| 待确认/确认/放弃 | 完成不占计算槽位；只能查看原片/参数、预览、确认或放弃后再改；真实 Chromium 放弃后删除参数保留、原片不变；再次生成并确认后新会话为 84 帧 | browser HTTP 用例、legacy/managed 替换回归 |
| 服务端冻结 | 同素材不同方案、scan、旧 cuts/open、整个项目 gate 被拒绝；纳管 A 参数修改、删除 A、公共项目参数与归档被拒绝；B 的 notes 修改可成功 | queue integration 的 legacy/managed 分支 |
| 幂等 / 竞态 | HTTP 重复提交同方案同 ID；伪造 actor 无效；不同方案 409；取消/放弃后新 ID；queued 取消不启动；完成报告与槽位释放间恢复不重跑 | queue core / integration / render stop |
| 投影/执行/完成故障 | 入队成功后报告写入故障可修复；claim 后恢复；取消意图优先；第三次中断有限失败；完成落盘但槽位未释放只收尾 | `test_mosaic_queue.py` |
| 真实服务重启 | queued 时正常停止服务并重启，参数仍在并真实产出 84 帧；running/composing 时 kill -9 服务，重启后同 ID 恢复 1 次并真实产出 90 帧，原片 SHA 不变 | [重启证据](cut-mosaic-background-queue-restart-2026-10-31.json) |
| 进程所有权/异常退出 | 原所有者退出、旧 FFmpeg 尚持锁时恢复器不抢占；实际 FFmpeg 的守护在父进程消失后结束整个所属进程组，核验没有同组活进程，再释放资源 | `test_mosaic_worker_lease.py`，含真实 60 秒 FFmpeg 提前终止 |
| 权限与错误 | 两个账号实际登录，第三个无权账号读状态/取消/确认均 404；请求 actor 不影响真实归属；账户失效、版本变化、源输入缺失、磁盘不足均失败且原片哈希不变 | queue integration |
| 前端异常 | 无效本机草稿不能挡住服务端冻结任务恢复；断网轮询保持冻结并恢复到待确认；取消后本机后续调整重开不被旧快照覆盖；既有快速切换/过期回调/关闭测试回归 | `test_mosaic_queue_ui.py`、`test_mosaic_ui.py` |
| 旧会话 / 资源协调 | 历史任务仍按旧报告读取；新队列不重跑历史结果；固定区域与片段预览保留 compute 资源门；原片准备和 frame lease 不依赖长任务 compute | render、tracking、queue integration 全量回归 |
| 维护 / 多进程排他 | 两个 dispatcher 对同 lease 排他；停止请求仍等待活 worker；维护不 claim、pending 保留、active 和损坏队列阻止 release；字幕 queue 文件未被复用 | `test_mosaic_queue_release.py`、integration、worker lease、既有字幕/制作完整回归 |

没有进行断电/文件系统损坏、真实 GPU SAM 峰值、线上压力或更高真实计算并发的验收；不据本地用例推断这些条件已安全。

## 独立 dev / 浏览器 / 媒体证据

实际入口为 `http://127.0.0.1:18883/home/`；独立合成项目 `44856f4afe564766b8cc8ea8d5350a71`，原始两条视频都是 112×64、30 fps、90 帧、带合成音频的独立复制件。原片 SHA 为 `6b2efa0f7bb4dc39e696394246f687c0afd5d3eb444c628e140b3668f9d635e3`。

- 最后一次独立浏览器实例：本工作树 `source/.dev-state/d1-mosaic-verified/start.py`，数据为同目录 `data/`，历史 PID 15106；启动/停止前均核对监听、命令和 cwd。最终队列三个任务都是 done（cancelled / completed-discarded / completed-replaced），确认没有 active/pending 后正常 SIGTERM；已核验 18883 无监听且无该 PID 子进程。数据、私有 0600 登录文件和日志保留，不随 Git 提交。
- 真实重启实例：`source/.dev-state/d1-mosaic-restart/start.py` 与 `restart_acceptance.py`；只操作脚本自己 Popen 创建、持有并 wait 的服务进程。queued 正常停服/重启与 running kill-9/重启均成功，脚本 finally 正常停止最后实例，18883 无监听。数据独立于上面的浏览器实例。
- 另有早期自有 `.dev-state/d1-mosaic-queue/`、`.dev-state/d1-mosaic-final/`，已停止，保留失败重试过程与合成数据；没有清理其他会话文件。
- [浏览器与媒体公开证据](cut-mosaic-background-queue-evidence-2026-10-31.json)：14 项真实浏览器断言、0 JS 错误、未确认源片哈希、两次 84 帧/带音频结果的 SHA 和最终替换状态。取消、失败与放弃保留源片；两次相同方案渲染哈希一致，但 task ID 不同。
- [排队截图](cut-mosaic-background-queue-queued-2026-10-31.png)、[完成待确认截图](cut-mosaic-background-queue-completed-2026-10-31.png)。截图仅作布局/可读性证据；帧数、哈希和音频结论来自实际媒体/报告断言。
- Paseo 浏览器无法连接本机监听（连接拒绝），其自有测试 tab 已关闭；本机 Chrome harness 未开启远程调试。随后使用项目既有 Playwright/Chromium 模式，独立 context 与真实 localhost HTTP 完成操作，不复用用户浏览器 Cookie。
- 8793、8801、8810、8821、8822 等其他现存服务未停止或重启。生产数据、账号、配置与发布入口均未写入；没有触发 Gemini、外部上传、FBBM 或正式制作 worker。

可复现的完整 HTTP + Chromium 用例已提交为 `tests/test_mosaic_queue_browser_http.py`，不依赖私有 dev 账号、缓存或服务仍存活。

## 服务器容量只读调查

沿用 `docs/server-test-deployment.md` 已证实的 SSH 连接，仅查询资源与瞬时状态；2026-10-31 11:08 +08：

- 宿主机 16 CPU、30922 MiB 内存；可用约 29506 MiB；swap 0；load average 1.10 / 1.13 / 1.03。
- 正式容器限额 14 CPU、24 GiB、512 PID；瞬时 CPU 100.10%、内存 93.8 MiB、19 PID。
- 测试容器配置限额 6 CPU、6 GiB、256 PID；此次没有对其启动、修改或施压。

没有代表性 SAM/合成峰值数据，所以保留 `max_active_tasks=1`。该瞬时快照不是容量承诺，也不能证明所有其他制作/字幕/下载竞争场景的峰值都已覆盖。未在线上跑压测、复制数据或更改配置。

## 交接给父会话

重点 review：入队/投影与完成/释放的写序；actor 公平与跨项目单用户上限；素材 gate / 纳管 mutation 的冻结覆盖；内部替换身份是否只豁免自身；取消与恢复先等待真实子进程退出；启动失败与关闭时 lease 生命周期；前端待确认及失联时不能解锁。

本分支提交后交父会话亲自 review；发现问题继续在本分支修复，不自行宣布用户最终验收、不发布生产。
