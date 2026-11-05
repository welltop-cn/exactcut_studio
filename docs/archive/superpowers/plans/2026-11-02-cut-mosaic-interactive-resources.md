# 剪切打码交互并发资源设计（父设计review已批准）

> 历史归档（2026-11-04）。正文保留当时的方案或执行状态，不代表当前版本；日常操作见[文档入口](../../../README.md)。

状态：2026-11-02，设计与实现review均已通过。以下“不更新18883”是隔离开发阶段约束；父独立45专项review后已按空闲屏障一次正常重启交付本地18883（PID59032，运行c93dc49）。原URL/数据根保留，334保护文件登录前SHA一致。详见 `docs/archive/verification/cut-mosaic-interactive-parent-review-2026-11-02.md`。不发布生产、不声称Linux CPU容量已验证。

**Goal:** A 最终生成时，B 真正执行 Gemini 识别、SAM 效果预览、编辑和最终入队；至少两个认证用户的交互模型工作可同时执行。

**Architecture:** 最终任务继续由现有持久队列和 `compute.lock` 串行执行；交互工作使用单独、至少允许两个用户并行的有界预算。SAM 不使用全站单个互斥锁，也不暂停 A。相同推理 key 的缓存生产使用独立租约和原子发布。

**Tech Stack:** Python 3.12、现有 JSON schema_version=1、fcntl 文件租约、现有 processing_child 进程组守护、Gemini SDK、真实 SAM 2.1 tiny / 1024、FFmpeg。部署目标无GPU，按CPU服务器交付；主线设备无关。MPS仅本地Mac兼容修复，独立提交，不作为服务器门槛。

## 不可退让的交互契约

1. A 提交后可立即退出并打开 B；B 的识别、预览和入队不等待 A 结束。B 入队后可继续操作 C。
2. 采用同时执行。不能用暂停 A、抢占 A、禁 SAM、切 fixed 或等待 A 完成替代。
3. 两个认证用户至少可以同时识别、同时预览；明确公布容量，不能用全站一个交互槽假装多用户支持。
4. queued/running 同素材冻结；先取消再修改；完成先确认替换或放弃结果。其他素材可编辑。
5. 新能力先在独立实例验收，父 review 批准后才允许安排更新用户 18883。

开发工作树：`~/.paseo/worktrees/1kps4qs6/cut-mosaic-interactive-resources`，分支 `feat/cut-mosaic-interactive-resources`，基于 `1a8d7c0`。原运行工作树保持 clean；避免静态 JS 或新 SAM 子进程提前读到修复。

## 已确认根因及修复前路径

- 用户 trace `2414421e68af4d9d87b46409e373adb9`：A running/composing 时，B scan admission 因 `compute.lock` 返回 429。增加重试不能解决长任务占锁。
- `mosaic_render.execute` 最终任务持有 compute/worker/batch leases；片段 `start(preview=True)` 和 `mosaic_tracking.start` 也抢同一个 compute，直接阻断 B 预览。
- `mosaic_scan.start` admission 抢 compute；`cutting_scan.detect` 完成 FFmpeg 代理后 close compute，再请求 Gemini。后续自动定位通过已有短帧租约抽图。
- `mosaic_locate.start` 不抢 compute，单用户可与最终任务并存；但整片、区域识别共用全站 `scan.lock`，会阻断第二个用户。
- `mosaic_session.open_session` 已用独立 prepare 锁；frame 使用短时 `frame_lease`，不依赖最终 compute。
- `prepare_track` 的 `inference-*` 目录目前直接生成、复用；允许并发后不能继续在同目录清理/写文件。
- `processing_cancel.scope` 当前覆盖 FD 列表，不能直接用它添加缓存/阶段租约，否则会丢 worker/compute/batch FD；扫描当前也没有完整的子进程租约继承。

## 实施与真实验收补充

- 隔离修复已完成真实A/B、两个认证账号的Gemini/SAM重叠、取消重提、旧single-track/旧cuts和最终输出完整性验收；完整回归1995通过/1跳过/10个既有fork警告。详见 `docs/archive/verification/cut-mosaic-interactive-real-2026-11-02.md` 及同名JSON。父实现review与用户服务更新仍未完成，18883保持原状。
- 实证追加发现旧 `cutting.open_session` 首次准备也握最终compute；已改独立 `cut-prepare.lock`，不能通过预先创建旧cuts会话掩盖该路径。
- MPS原CPU状态卸载路径的非阻塞跨设备搬运，在同输入第二帧可返回NaN。保持模型、1024、MPS计算、CPU卸载和并发额度，只在独立SAM进程内将这些搬运同步完成，60/60对照通过。scope退出恢复Torch方法，不在web进程patch；新MPS identity `blocking_v1`，旧MPS派生缓存重算，CPU/CUDA旧缓存兼容。严格非有限检查及基础设施failed保持。
- 保留合法fixed20秒/600帧和tracked区域跨500帧包络，实际FFmpeg逐帧完整性通过。没有新增fixed/包络十秒限制。

## 容量诊断结果及边界（初始诊断；同步搬运修复后已复测）

同步搬运修复后另以相同1080×1920/30fps/300帧输入复测三路：均300/300非空，耗时319.63/317.70/319.77秒，整体323.12秒，真实mask窗口交集308.264秒，OS free最低25%，无OOM/保护中止。采样MPS driver约9.993GiB/进程，RSS历史峰约4.16–4.17GiB；不能与各类计数相加估物理显存。详见 `docs/archive/verification/cut-mosaic-interactive-capacity-sync-2026-11-02.json`。下面保留最初诊断，不能把其耗时当新版速度承诺。

机器：Apple M2 Max，12 核，64 GiB 统一内存；本次开始 OS free=52%。

独立合成 1080×1920、30fps、300 帧（10 秒），每个进程跟踪全部 300 帧；SAM tiny 原权重 SHA `7402e0d864fa82708a20fbd15bc84245c2f26dff0eb43a4b5b93452deb34be69`，推理 1024，device=mps，offload_video/state_to_cpu，未修改模型/降采样/降分辨率。三进程同时启动，单独输出目录，600 秒保护超时，OS free<18% 时只终止本诊断自有进程组。

| 诊断角色（尚非应用 admission） | 实际帧 / 非空 mask | SAM耗时 | RSS历史峰值 | 采样 MPS tensor峰值 | 采样 MPS driver峰值 |
|---|---:|---:|---:|---:|---:|
| final_role | 300 / 300 | 296.00 s | 3.733 GiB | 1.194 GiB | 9.993 GiB |
| preview_alpha | 300 / 300 | 269.56 s | 3.730 GiB | 1.194 GiB | 9.993 GiB |
| preview_beta | 300 / 300 | 291.27 s | 4.000 GiB | 1.194 GiB | 10.059 GiB |

三路实际 mask 写出区间交集 **245.813 秒**，整体298.30秒；每进程报告 recommended device memory=48 GiB，OS free 最低22%，未触发保护、无OOM，输入 SHA 不变。并发采样进程 RSS 和的峰值为11.166 GiB。

**不能把 RSS、MPS tensor、MPS driver 相加当物理显存。** driver包括缓存和框架分配；它明显大于 tensor，不能只用1.194GiB估算每个模型。MPS allocator为100ms采样，RSS历史峰值与各模型峰值也未必同一时刻。此处无权威设备全局 used-memory 计数，仅有每进程 allocator、设备 recommended、OS pressure。

这是一组低熵合成画面，并非生产真人长片或其他主机负载的峰值认证；但已覆盖现有单区域300帧上限和肖像输出尺寸，证明三路模型确实同时推进。整片最终编码、真实跨用户鉴权、长任务 A/B 业务时间线仍须独立验收，不能据本诊断声称完成。

私有原始采样：`source/.dev-state/interactive-capacity-20261102/capacity-evidence.json`；公开摘要见对应 verification JSON。

## 方案 A：独立进程 + 分离预算（建议本轮采用）

下列默认限制是可配置的逻辑额度，不代表用户认可“最多两个人”或CPU服务器已支持该峰值。服务器无GPU，CPU并发profile必须依据其CPU/RSS证据核定，不能套用M2Max显存预算。本地device=cpu的3×6帧认证并发冒烟已通过，真实两预览mask窗口重叠10.069秒；尚无Linux服务器等价容量/完整A/B验收。并发参数分别校验用户和全局额度，扩容仍需容量实证。额度不进入tracking配置或缓存identity。交付明确实际验证用户数，不能声称无限并发。

| 工作 | 全实例并发 | 每认证用户 | 持有范围 / 说明 |
|---|---:|---:|---|
| 最终成片 | 1 | 1 | 保留 queue.max_active_tasks=1、compute.lock、原 FIFO/跨用户轮转 |
| Gemini识别任务（整片/区域/旧剪切识别） | 2 | 1 | 从 admission 到该任务结束；两个用户可并行；不拿最终 compute |
| 本地识别预处理 | 2 | 1 | 仅代理 FFmpeg；结束即释放，远端等待不占该槽 |
| 交互效果预览（片段/旧单区域） | 2 | 1 | 从 admission 到完成/真实停止；与最终任务配额分离 |
| SAM实际模型进程 | 最终1 + 交互2 | 交互1 | 由最终/预览预算约束，不加全站 SAM mutex；各进程模型状态独立 |
| 准确帧读取 | 保留现有短帧租约 | 现有规则 | 短暂解码串行不等于整条识别/预览串行 |

- 最终A不计入该用户的交互预览或Gemini额度，A不能占走两个交互名额；一个用户也不能占满两用户预算。
- 同用户可一条Gemini工作和一条预览并存；不同任务使用不同角色预算。第三个并发预览超过该profile容量时明确反馈“交互预览容量已满”，不把它伪装为等待A。不能声称无限用户均无等待。
- 满额时在 admission 前拒绝，不创建幽灵running任务；相同请求幂等返回原任务。第三用户仍可编辑和提交最终队列。
- 本地预处理与预览CPU编码有明确进程数量上限。沿已有 bounded_ffmpeg，交互输出encoder使用2线程、filter/input约束保持；SAM当前4CPU线程。最终4线程+两个SAM可能使用12线程，再叠两个代理约16线程，允许OS调度带来变慢，不承诺12核硬隔离。
- 不把3路预算自动套到6GiB容器、CPU推理或其他机器。其他profile必须重新容量验证；不足时明确报告/要求扩容，不能静默改成全站串行。资源不足/worker基础设施失败应明确失败，不能把因并发压力导致的SAM失败当作成功无打码结果。
- 资源预算作用于同一应用data root。并存的其他dev不受本实例租约约束；容量记录包含当时主机负载，不能把实例上限说成主机全局上限。不会改其他dev。

Gemini继续使用现有同一 `ai_rate.acquire`、同一key及跨进程持久桶。独立dev使用父已授权的集中 `concurrency.ai={requests_per_minute:6,burst:2}`，不新建AI限流域、不绕过预算；其他并发配置显式保持原值，避免“仅加concurrency.ai”意外启用其他lane并行。真实请求数和时间逐次记录；vendor429按现有错误处理，不伪造成功。

## 方案 B：常驻 SAM 模型执行器（暂不建议本轮）

多会话共享一次权重/模型缓存，可能减少三进程重复driver缓存和冷启动，但需要新的IPC、进程生命周期、每会话状态与GPU并发安全验证。目前未验证官方predictor的该共享方式，不能据此承诺省多少内存或改变输出语义。除非方案A后续真实业务证据表明容量不可接受，否则不在本轮引入新的模型守护服务。

全站单个交互槽、单GPU大锁、暂停A让B跑、先等A完成均不是可选方案。

## 片段长度、分辨率与解码

- 保持SAM1024及既有每个跟踪区域≤10秒且≤300帧、最多8区域；每个预览任务仍顺序处理其区域，两个用户之间并行。多区域会增加耗时，不把每区域峰值乘8当同时驻留内存。
- 片段合成保持长边≤640；最终完整输出保持原分辨率。不能为了并发把SAM改为512/768。
- 保留当前精确解码。seek优化不是本轮必需；如后续优化，独立提交并以真实逐帧/分数fps/音频对照证明等价，不拖延资源分离。
- **不新增fixed十秒或跨targets包络限制**；先核对前端按clip/effect key收集区域及已有API合法范围，保留合法fixed长范围和多区间预览。沿低分辨率流式编码保证内存有界，不静默截断、不改最终计划，时限覆盖完整合法操作。
- 单模型保留900秒边界；预览多区域顺序运行，整体限时需要覆盖合法最多8区域及合成，不能只给单区域时间就误杀合法请求。设计不能承诺10秒素材一定快速完成：本次三路满300帧已约270～296秒。

## 锁、缓存与取消顺序

1. admission：现有batch读租约 → 素材gate → job state锁 → 用户角色租约 → 全局角色槽 → worker lease；均非阻塞抢占，失败反向关闭。不得在素材gate、queue锁、job state锁里等GPU/模型/网络。写完整running报告后释放gate/state，启动线程。
2. final仍：worker → compute → 短素材gate校验 → batch shared → 执行；取消等待compute的现有规则保留。
3. 识别：worker保留任务/用户/角色/batch租约；代理阶段取得预处理租约，只在FFmpeg运行期间存在，**进入Gemini前释放**。不能把已close的FD留在thread-local列表；精细复查/后续定位再次走短预处理或frame租约。远端阶段不占CPU/model槽。
4. 预览：持有预览用户/角色/worker/batch租约；独立模型进程可同时与最终SAM执行。每次推理开始前按 session+source/reset+geometry+现有tracking配置取**同key专属锁**；不同key/素材/用户互不阻塞。
5. 同key锁在检查复用、生成和发布全过程持有；等待可取消。锁只能合并同key工作，不能变成跨用户全局推理锁。
6. 每次生产写唯一临时目录；校验frames、mask数量/尺寸及完整result后原子重命名发布。完成缓存不可原地清空、改写；取消只删自己临时目录，已发布缓存保留。旧可验证cache继续只读兼容。
7. 推理前后重校验source/reset。缓存键继续包含source/reset/geometry/模型配置；资源额度放在独立配置段，不能因改变并发额度失效所有模型缓存。
8. `processing_cancel`新增追加阶段FD的上下文，嵌套时保留外层worker/compute/user/batch/角色FD，退出只移除自己的阶段FD。SAM、代理FFmpeg、片段编码都让processing_child保留相应租约；父进程死亡杀自有进程组，实际子进程退出前名额不能释放。
9. 正常取消仅作用该job进程组；不能pkill所有模型或取消A/C。UI关闭片段预览终止该预览，最终任务关闭仍继续。旧单区域跟踪入口也须纳入同一预算、子进程守护及明确停止路径。
10. 预准入只读任务携带显式内部permit（batch/session/job/request/source/reset/actor），从gate内签发贯穿scan/locate/preview/current，仅传给原worker、不从请求body接收。它们可在最终入队后完成，但外部新交互仍严格拒绝；取消/放弃reset及源版本改变使permit失效。补race测试，旧preview失败回调不得覆盖新final UI或解锁。

## 资源反馈与公平

跨用户配额在服务端使用认证用户名，不能信任请求body或默认把所有预览归admin。需要调整HTTP adapter把actor传入整片、区域、片段和旧跟踪入口；直接测试调用的默认actor规则要明确。每用户单角色名额避免一个人抢满两槽；更多用户超出已批准profile容量时显式反馈，不声称无界并发或必然FIFO。

沿用pre-admission retryable错误约定；若需要新增resource枚举，必须同时更新服务端/浏览器白名单、诊断校验和契约测试，不能只改一侧。错误文案区分“当前用户已有交互任务”与“本实例交互容量满”，不得把最终A持有compute当作B识别/预览拒绝理由。

## 后续实施文件与验收（批准后开始）

- 新资源模块负责角色/用户租约及集中配置；HTTP adapter传认证actor；`processing_cancel`负责可嵌套FD继承。
- `cutting_scan`、`mosaic_scan`、`mosaic_locate`迁移Gemini/预处理边界；旧剪切扫描也纳入，统一原AI bucket。
- `mosaic_render`、`mosaic_tracking`迁移预览预算；独立推理缓存模块负责key锁、可取消等待、原子发布和复用。
- 区分业务失目标（有效结果中个别box=None及告警）与基础设施失败。OOM、租约/缓存/进程失败必须failed，不能skip_region后产出成功未打码；不泛化改写所有既有容错。
- 保持最终队列、同素材冻结和版本确认规则；补无tracking能力的明确提示，不能改已有tracked选择语义。

| 必须验证 | 证据要求 |
|---|---|
| A资源持有时B识别/预览 | 准入红灯先复现compute429，再真实Gemini+SAM+FFmpeg绿灯 |
| A最终活跃、B完成预览后queued | 足够长真实复制样本的A媒体执行，不用sleep/占锁替代；B真实模型/播放完成时间与queued；B完成在A结束前 |
| 两个认证用户 | 独立账号/项目/context，同一rate bucket；两用户真实Gemini窗口和SAM窗口分别有交集 |
| 有界及公平 | 第三预览超额、同用户重复/不同key、不同用户各1、队列实际最终并发1 |
| 取消/关闭/父死亡 | 只停目标组；A/C继续；名额实际释放后可再用；无遗留进程/泄漏租约 |
| 缓存并发 | 同key只推理一次，不同key实际重叠；取消发布前/后，父死亡、损坏缓存都可恢复 |
| 精确媒体 | 晚片段seek、分数fps、原音频、帧数、源SHA；不降SAM分辨率或缩范围伪成功 |
| 现有规则 | 同素材queued/running/待确认拒绝编辑，先取消/确认/放弃后编辑；旧入口不能绕过 |
| 回归 | 复用原1943项全套及新增用例；完整结果、skip、warning如实记录 |

现有窄基线14项通过。资源红灯测试文件为 `source/tests/test_mosaic_interactive_admission.py`；另有无跟踪能力提示红灯。红灯使用明确模型double，只验证阻塞与并发准入；真实关键链路在独立实例执行，不以mock替代。

父批准后的执行顺序：资源/FD模块及准入红灯转绿 → 显式permit与race → 同key缓存原子发布/取消/父死亡 → UI与基础设施错误分类 → 独立真实A/B及双认证用户Gemini/SAM时间重叠 → 完整回归和父review。保持同一6RPM/burst2桶。补多区间、晚时间、效果修改、开关预览、取消后重提和完整输出检查。实际预览耗时与内存压力如实记录；容量诊断不是速度承诺，非空mask不是遮挡质量证明。只有父实现review批准后才安排更新18883。
