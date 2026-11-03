# 素材生命周期：多轮线上回归与 Grok 复审

## 范围与结论

2026-10-11，沿用 `hookqa212220a` / `hookqa212220b`、剧 101494 及原四份素材配置。没有创建用户，没有提交 FBBM，没有删除 OSS 对象或改桶配置。线上破坏性故障未注入。

首轮线上测试版本为 `us-east-20261011-reviewed-3b5cfe1`；最终发布版本为 `us-east-20261011-recovery-282934d`，包含收尾防护 `d3b25c6` 和失效候选队列修复，详见文末。

- 第一轮：权限、CSRF、修订冲突、幂等重放/参数不匹配、非法草稿/重复及外来素材 ID、暂停/归档/恢复、逻辑删除、修复身份稳定等。
- 第二轮：四项目同时开始、重复提交、队列中归档/解归档/恢复，全局 render 上限为 3；8 条主片全部完成。
- 第三轮：候选制作中删除素材、禁止签名、恢复后重新确认产生新版本，旧候选不得成为 current；完成项目重复确认不产生额外版本或渲染。
- 主报告 69 项、交错变更报告 8 项，合计 77 项通过。浏览器双窗口 CAS 另单独通过：旧窗口保存冲突、输入保留；测试标题已恢复。
- 8 条主片 SHA256 均与原测试输出相等；16 份主片/预览的时长、编码、分辨率和 OSS 回执 hash 检查通过。没有用“能播放”代替产物一致性。

## 性能实测

服务端采样详情 HTTP 中位 17 ms、最大 343 ms；render 并发最高 3，服务器内存使用峰值 3081.6 MiB（/proc/meminfo 的 MemTotal−MemAvailable，不是容器 RSS）。第二轮最终四项目均 review；第三项目还交错了删除/恢复，不将总耗时当成纯性能基准。

| 项目 | 两条素材准备至预览总秒数 | 对应排队秒数 |
|---|---|---|
| 482188e99cc9431e9b2a8d425d56306f | 225 / 476 | 0 / 216 |
| 7f57c3523593425f87599aefd2d1da91 | 221 / 260 | 0 / 0 |
| aa12bd2ad2b54ba197f66516f22af6a7 | 414 / 680 | 212 / 425 |
| a425fef3d1474cd4b3cc3a5720ad1760 | 435 / 512 | 218 / 250 |

实际 Tabbit/Chromium 两组各 10 次切换、5 次 reload。基线未加载视频；对照组另一标签进入成片预览并实际播放，视频 origin 为 OSS，播放时间从 1.89 秒增至 29.14 秒。已缓存视频可能不持续传输，因此此组是播放/已预加载并存的交互测试，不宣称每个采样点均在冷下载，也不代替用户 Mac mini 的设备实测。

| 指标 | 基线 | OSS 播放/预加载并存 |
|---|---:|---:|
| 切换完成中位/最大 ms | 65.5 / 339 | 68 / 843 |
| 刷新完成中位/最大 ms | 1624 / 1680 | 1617 / 1620 |
| HTML TTFB 最大 ms | 266.1 | 257.7 |
| 详情 TTFB p95/最大 ms | 459.2 | 372.0 |

切换包括缓存命中，不能把缓存 DOM 耗时当成网络 TTFB。样本少，p95 为本组最大值。均低于暂定 2 秒 TTFB 门槛，媒体组也未超过基线两倍。

## Grok 交叉复审与修复

Grok 4.7（xhigh）独立只读复核。它确认入口 guard 不新增锁或持锁 hash；正常执行身份、恢复和启动后失败保持行为，日志打开前尚未持久化的下一 attempt 通过显式 local_failure 例外处理。旧事件 superseded 时不能利用此例外。

隔离 fixture 复现：旧核实失败后新核实已 ready，再手动调用旧 scheduler finish_job 会把子 publication 降为 unverified，父恢复再合并降级。正常单 worker 自然触发尚未证实，属于迟到回调防御缺口。修复在任何副作用前匹配父 outbox、交付/版本/绑定及持久化 attempt/token，拒绝过期身份；legacy 路径保持。

最终新增 7 项回归包含三个真实 fork/os._exit 窗口、旧任务收尾、旧 attempt、合法尚未持久化的启动失败、superseded + local_failure 反向用例。修复前 Linux 最终镜像独立脚本 4/5 通过、1 项失败；修复后最终 282934d 镜像独立验证 5/5 通过。

Grok 另确认两项手工改坏 media-state.json 的引用测试不是应用自然产生的状态：current 指向缺失版本、queued 候选 outbox 被外部删除。当前不是全面关系校验；保留后续加固事项，不把这两项列为通过。正常候选和 outbox 同一次原子写入，应用没有删除 outbox 键的路径。

维护 CLI 对内部交付子项目不是正常受管审批入口。后续可在入口明确拒绝 internal 身份；本轮未在线上运行该 CLI。Grok 初稿误以为交付开关关闭，已告知两开关均开启，并收到再次复核“不阻断本次修复”。

## 证据及限制

- 本机脱敏验收数据：`~/.codex/artifacts/exactcut-soak-20261011/`。
- 服务器：`/srv/exactcut/acceptance/soak-20261011/`，测试脚本位于 preparation，保留所有测试对象。
- 浏览器第一次双窗口操作的收尾超时且丢失 receipt，没有计入通过；第二次独立获得 CAS 成功证据并恢复标题。最后关闭 scratch 标签亦触发工具超时；证据此前已导出，保留页重新附着确认视频暂停。按钮名称误用造成一次 locator timeout，纠正为页面实际“保存并校验”后测试通过。
- 一次从仓库根运行 pytest collect 导致模块找不到，使用项目 source 工作目录后 7 项收集成功；不是应用运行失败。
- 真实 FBBM 提交/交付、冷源站重新下载、任意文件损坏的全面恢复、所有可能故障组合均未声称通过。详细前轮覆盖见 full-reviewed 报告。

## 最终修复发布

d3b25c6：Linux 972 passed / 2 skipped，190.25 秒；最终镜像 network none 只读独立故障复验 5/5 通过。镜像 sha256:a8121cb4d2f0852a8a32da710870f62876f429e060f5b9f08beacef7e826c19c。备份 `/srv/exactcut/history/before-soak-20261011T010344Z`，858 份元数据及 SQLite。

发布尝试被排空门槛拒绝，未发生版本切换：旧候选 `d48f32f76bb2424f86bceceba247ccc7` 已 superseded，但派生 render job `3109afab026e582abb8e6fb874400d96` 仍 queued，内部 child `b38f8abc5bc6420bb59ba100e8c11097` runtime 仍 producing。旧 cancel_invalid_children 只信号终止已有进程，没有终结尚未启动的任务；batch_control 将此任务永久视作 paused，release phase_blockers 因而拒绝。该项说明前述 77 项业务断言没有覆盖“最终所有内部队列排空”，不能称完整生命周期全部通过。后续修复和正式维护恢复已完成，详见下文。


### 取消恢复修复 282934d

- worker 在父权威状态明确失效后，终结候选的 queued/waiting_resource 派生阶段，释放预约并更新内部 runtime；保留任务文件，避免旧 fetch 重建确定性后继。不取消交付、健康候选或可识别的活进程。每父恢复仅一次扫描 jobs，按 child 分组，避免按历史候选数重复全目录扫描。
- 额外实证否定了通用在线恢复方案：真实 worker_tick 在已读取可执行 control 后，CLI 若直接写 interrupted，旧 admission 仍可能继续覆盖为 running。故命令只允许 maintenance 标记存在且 worker_enabled=0 的维护执行；没有在线执行该有竞态方案。
- 精确 CLI 默认 dry-run；只接受永久 superseded、不再是 current/candidate、单素材、completed fetch、确定性 render ID、queued 且无 attempt/pid/token/admission、无其他未终态任务或进程记录的指定 child。仅终结 job 与 runtime 投影，不改 media-state/current/outbox/产物。
- linux-ops retire-candidate：封存镜像 dry-run；写屏障和全库排空检查；停止当前服务；释放 mutation.lock 但保留维护；隔离网络执行；无论结果恢复原版本；phase_blockers 与 active_records 均空才撤维护。失败保留维护，不切换 current；后续发布仍走原 gate。
- Grok 首稿遗漏了旧 recover_jobs 最后的 runtime 收尾循环，经真实测试和源码纠正后认可。最终仍显式更新 runtime，避免依赖恢复后下一 tick；它已批准进入全量回归与真实镜像演练。
- 最终构建、独立真实 Docker 恢复演练和生产维护恢复均通过，证据见下节。


构建也包含工作区近期提交 `2f0b574`（侧栏列表独立滚动）和 `7377ffe`（项目卡片紧凑布局）。已逐条审阅 CSS 及桌面/窄屏浏览器回归；本地最终快照与 282934d 的 index.html 和 linux_ops.py 逐字节一致，共收集 1000 项，全套退出 0。发布后另检查真实侧栏滚动和动作布局。


### 最终发布和恢复实证

- `282934dd79552bcc40501a802d6029be3d4e98a4`：本地 1000 项；Linux 996 passed / 4 skipped，192.21 秒（Docker CLI 两项、Playwright 浏览器两项；本地浏览器已通过）。镜像 `sha256:8672328f00bffb1d67fe97891a07df5f4e0e897d61071e7ce2f5c3c421ae61be`。
- 同一最终镜像只读、无网络的核实回调故障复验 5/5 通过。
- 独立真实 Docker 演练使用真实旧 3b5cfe1 镜像和新 282934d，独立合成 fixture、端口 18893、独立凭据；复现旧候选 blocker，经维护停止/恢复原镜像，再正常 publish，新旧权威文件字节一致。结果 `/srv/exactcut/acceptance/retire-us-east-20261011-recovery-282934d/exercise-result.json`。未挂生产数据或凭据。
- 生产维护前备份 `/srv/exactcut/history/before-soak-20261011T012752Z`，858 份元数据及 SQLite。通过封存版本的 linux_ops.py `retire-candidate` 正式入口恢复指定旧任务；authority SHA256 前后均 `03ba38e08db09becb9a747916f35c1b45dff7c7a0b4d493c2cbaba3e3e21736e`。原镜像正常恢复，active_tasks/phase_blockers 均为空、maintenance=false。
- UTC 2026-10-11 01:28:19 正常发布 `us-east-20261011-recovery-282934d`。发布后 healthz=ok、worker_alive=true、maintenance=false。
- 发布后真实浏览器侧栏 9 张卡片，单卡高度约 117 px，状态与动作按钮无重叠；列表可视高 409 px、内容高 1090 px，滚动到 681.5 px 时页面 scrollY 仍为 0；120 秒 OSS 预览实际播放。
- 最终四配置重跑 `/srv/exactcut/acceptance/soak-final-20261011/` 已实际等待旧候选 render queued/no-attempt，再删除/恢复确认。即时检查旧 fetch completed、旧 render interrupted、旧 runtime partial（非活动终态）、old version superseded，通过，没有手工编辑生产 JSON。
- 最终重跑主报告 77/77、交错变更 9/9，共 86 项通过；两报告 completed=true。8 条主片与历史基线 SHA256 全部相等，16 份主片/预览格式、时长及回执 hash 检查全部通过。逐个项目确认内部候选任务终态、runtime 非活动；全库 active_tasks=[]、phase_blockers=[]，healthz=ok、worker_alive=true、maintenance=false。

归档卡片另经真实浏览器验证：两枚操作按钮、卡片高约 144 px，状态徽标与按钮无重叠；完成归档/复原后已恢复原测试项目，归档计数回到 0。

### 最终线上重跑数据

最终版本四项目仍使用原四份配置、两名既有 QA 用户。主报告总耗时 738.8 秒包含下载接口逐条读取校验；成片时间与排队时间如下。本轮刻意删除恢复一个已排队候选，不能将其与无交错操作的性能基准直接比较。

| 项目 | 两条素材准备至预览总秒数 | 对应制作排队秒数 |
|---|---|---|
| 39b4f695712a4c44ba9a0af940e821d2 | 222 / 478 | 0 / 217 |
| d19cef6afd90487fa9d8ce1c715b5303 | 222 / 261 | 0 / 0 |
| b3910f0897064d9d8aa6567c739b14e7 | 652 / 677 | 451 / 423 |
| ed432f91724a4d05a9fa735481ffd1ac | 433 / 514 | 214 / 251 |

- 详情 API 中位 20 ms、最大 824 ms；render 最高 3；服务器内存使用峰值 3170.2 MiB（MemTotal−MemAvailable），CPU 采样峰值 70.65%。这些不是容器 RSS 或独占进程 CPU。
- 真实网页在最后项目制作时显示每素材当前步骤、步骤耗时、总耗时及制作排队时长；归档卡片测试后项目已复原。浏览器标签已释放所有权并保留。
- 最终证据目录：服务器 `/srv/exactcut/acceptance/soak-final-20261011/`；本机 `~/.codex/artifacts/exactcut-soak-final-20261011/`。包括主报告、交错变更、媒体格式、产物对比、取消即时证据、最终健康与排空结果。
- 本轮未提交或核实真实 FBBM 交付，没有新建用户、删除 OSS 对象、手工改写生产 JSON 或绕过发布门槛。排空验收只证明本次覆盖的场景，不代表任意故障组合均已验证。
