> 历史归档（2026-11-04）。正文保留当时的方案或执行状态，不代表当前版本；日常操作见[文档入口](../README.md)。

我先分段读完 `review-input.md`，只依据材料做审查，不改文件、不跑命令、不联网。材料很长，我继续按段读 diff 和调度、阶段推进相关实现。总体结论：可以进入受控的生产验证，但不要直接按 render=3 / fetch=2 / upload=2 放量。主调度路径上未发现会稳定双开同一 batch、或让 poll 空等占死执行位的 P0；下面 6 个 P1 会在暂停、大文件、结果损坏、待核实、旧 CLI 和配置未落地时让验收失败。

1. **P1** 暂停中的执行器不占 lane 名额，内存可以超过「同时 3 路 render」的预算。  
   **触发：** 生产 `max_active_children=8`、`render=3`。先暂停若干 render（进程变为 `T`，ffmpeg 仍占内存），再提交新的 render。lane 计数只加非 `T` 进程，总量只卡到 8，于是可以出现 3 个在跑 + 多个已暂停的编码进程。  
   **证据：** `resource_usage` 仅在 `child_state != 'T'` 时增加 lane 计数，但 token 无论是否暂停都计入（`review-input.md` 约 1856–1876 行）；`reconcile` 用同一规则恢复（约 490–515 行）；生产总量 8、render 3（约 376–382 行）；测试 `test_paused_workers_still_consume_global_resident_budget`、`test_resuming_resident_at_global_cap_does_not_require_an_extra_slot` 把这种行为固定了下来。  
   **最小修正：** 非僵尸的 render/fetch 子进程，不论 `T` 还是 `S`，都计入对应 lane；恢复时同样受 lane 上限约束。  
   **验收：** 暂停 3 个 render 后，第 4 个 render 不得启动；`ps` 中带 ffmpeg 的 resident 进程数始终 ≤ 该 lane 上限。记录 RSS。

2. **P1** 调度器在全局锁内对整集正片做 SHA256，会堵住所有用户的准入。  
   **触发：** 任一 render 准入或恢复核对。`worker_tick` 持有 `LOCK` 与 `batch_control.LOCK` 期间调用 `fingerprint()`；render 阶段对 `download_plan` 里每个正片 `file_sha256`。正片按 `episode_budget` 可达数 GiB，这段时间别人无法准入，Hook 预览也拿不到同一把锁。  
   **证据：** 锁范围约 1921 行起；指纹调用约 2001–2002 行；哈希循环约 1324–1333 行；`recover_jobs` 也在这把锁里（约 1955 行），`verified()` 会再哈希成片。  
   **最小修正：** 哈希放到锁外，或只采用子进程已经写入的摘要；调度线程只比较路径、大小和已存摘要。  
   **验收：** 对 ≥1 GiB 正片提交 render，同时另一用户提交任务。后者的排队记录应在 1 秒内出现，而不是等哈希结束。

3. **P1** fetch/upload 已是 `completed`，但阶段结果校验失败时，后续阶段既不创建也不重排。  
   **触发：** 任务已完成且带 `attempt`，`stage-results` 损坏、被删，或成片/正片字节与结果不一致，而下一阶段文件还没落盘。恢复逻辑直接 `continue`。任务表面是完成，render 或 poll 永远不会出现。  
   **证据：** `recover_jobs` 约 1726–1731 行：只有「没有 attempt」或 `verified()` 成功才 `advance_stage`，失败分支没有改状态。  
   **最小修正：** 校验失败时把该 job 标为 `failed` 并写明原因，或从当前阶段重排；禁止无动作跳过。  
   **验收：** 删掉一个已完成 fetch 的 stage-result 再跑恢复。job 必须变为失败或重新排队，且日志能看出原因，不能停在 `completed` 且没有 render。

4. **P1** 「待核实」没有放行入口；受保护的 CF 请求也不再做一次重新登录。  
   **触发：** poll/upload 在 intent 已写成 `pending` 之后崩溃、回读超时，或 token 过期返回 100006/100009。状态变为 `unverified` 后，自动重跑和 `queue_publish` 都会拒绝。守护模式下 `attempts=1`，原来「先重新登录再试一次」的条件 `attempts > 1` 不再成立，错误体直接返回。本材料里没有「核实后沿原 record/oss_key 继续」的接口。  
   **证据：** `queue_publish` 约 2143–2144 行；`delivery_blocked` 约 2237–2238 行；`CfAdsClient.request` 约 1018–1036 行。  
   **最小修正：** 增加管理动作：只允许沿已保存回执里的 `feishu_record_id` / `material_id` 继续 poll，禁止新的 ingest/上传。100006/100009 在守护模式下仍重新登录一次，登录失败再进入待核实。  
   **验收：** 模拟回读超时后，第二次 poll 的 ingest 次数仍为 0；执行放行动作后只再查原记录。模拟过期 token 时应看到一次 login，而不是直接 `unverified`。

5. **P1** 独立 CLI `execute --publish` 仍是旧同步发布，没有 intent→执行→核对。  
   **触发：** 有人用该 CLI 发布；进程在 OSS 已写入、飞书记录未落账时杀掉再跑一次。网页阶段路径会停在待核实，这条路径不会。  
   **证据：** 实施记录写明这条边界（约 230、310 行）。`workflow.publish` 已走 `upload`+`poll` 保险（约 2391–2398 行）；`cli.py` 的 diff 只包了 `run_child` 和 `ResourceDeferred`（约 528–545 行），没有把 `--publish` 接进 intent。  
   **最小修正：** 该入口改调已有保险的 `workflow.publish`，或直接拒绝并提示使用阶段上传。  
   **验收：** 对这条 CLI 做「上传后丢响应再执行一次」，外部创建次数必须仍为 1，或命令直接非 0 退出且没有任何外部写。

6. **P1** 仓库里的生产容量不会自动作用到现网配置；漏写总量时默认也不是 1。  
   **触发：** 现网 `/srv/exactcut/config/runtime.json` 在升级前已经存在。`production.json` 只是初始化模板。运行配置没有 `concurrency` 时，`settings()` 把每个 lane 当成 1，把 `max_active_children` 当成各 lane 之和（4），`enabled` 默认为 true。这时 fetch/render/upload/poll 可以同时各跑一个，既不是默认文件里的全局 1，也不是生产模板的 3/2/2。  
   **证据：** `default.json` 总量为 1（约 353–365 行）；`production.json` 为 3/2/2/1、总量 8（约 376–387 行）；`settings()` 回退公式约 573–588 行；实施记录约 248 行说明改模板不会覆盖 runtime。  
   **最小修正：** 缺少 `max_active_children` 时默认 1。进程启动时把实际生效的 lane、总量、`enabled` 打进日志；与本次验收期望不一致就拒绝放量。  
   **验收：** 在运行配置缺 `concurrency`、只有 lane、以及完整生产段这三种文件上分别启动，核对日志里的生效值。放量前记录 runtime 文件的哈希。

7. **P2** upload/poll 不占磁盘预算；render 在同一块盘上按三份峰值相加，会提前拒绝。  
   **触发：** 磁盘接近 `reserve_bytes` 时提交 upload，预约字节为 0，账本挡不住它的本地写入。render 把 project、`production`、`TMPDIR` 各记一份完整预算，三者 `st_dev` 相同时再加总；容器里这三处经常是同一块盘，本可放下的任务会被拒绝。账本本身会在失败、结束、资源等待和重启时释放，主路径未见超卖或泄漏。  
   **证据：** `job_budget` 末尾 `return {str(root): 0}`（约 1540 行）；三目录累加约 1536–1539 行；按 `st_dev` 相加约 1445–1455 行；`reserve` 成功才入账（约 1458–1466 行）；释放与重建约 1469–1504、1659、1676 行。  
   **最小修正：** upload 至少按成片大小预约；同盘 render 预约一份输出峰值加一份明确的临时峰值，并在注释里写清组成。  
   **验收：** 同盘剩余空间只够一份 render 预算时，render 可以启动；两份 render 预算必须拒绝。upload 进行中把剩余空间减到成片大小以下，第二个 upload 必须拒绝。

8. **P2** poll 容量、暂停恢复顺序和排队可见性与计划不一致。  
   **触发：** 计划写生产 poll 不限，模板写成 `poll=1`。多个 batch 同时需要查 FBBM 时，查询串行；一次飞书超时会挡住后面所有 poll，但不会挡住别的 lane。暂停恢复按 batch 目录名排序，不走 owner 轮转。diff 里没有队列位置字段或接口。  
   **证据：** 计划约 122 行；`production.json` 约 381 行；`reconcile` 使用 `sorted(glob)` 后的 `waiting`（约 474、502–515 行）。准入侧的 owner 轮转是另一套逻辑（约 1828–1847、2047 行），测试覆盖了跨 tick 公平。  
   **最小修正：** 把生产 poll 提到一个小上限（例如 4）并仍受总量约束；恢复队列按 owner 游标排序；给排队 job 增加 `queue_position` 只读字段。  
   **验收：** 4 个 batch 同时 poll，应能看到多于 1 个 poll 进程，且任一 render 仍能启动。连续暂停/继续时，后一个 owner 不能总排在字典序靠前的 batch 后面。

9. **P2** 关掉开关或退回旧代码，都不能把已入队阶段任务变回旧的 `produce`/`publish`。  
   **触发：** `concurrency.enabled=false` 只影响新提交：`prepare_staged_job` 直接返回原 job。已经写成 `workflow fetch|render|upload|poll` 的队列会继续跑阶段机，只是全局同时只启动一个。若发布回退到没有这些子命令的旧代码，这些队列命令会启动失败。配置回退本身能保持 schema 1 和新字段可选。  
   **证据：** `prepare_staged_job` 约 1590–1600 行；`enabled` 时的全局门槛约 1986–1987、510–511 行；实施记录约 244–248 行；`stage_command` 约 1581–1585 行。  
   **最小修正：** 回退文档写明必须先等队列排空，或在 `enabled=false` 时把仍为 `queued` 的阶段 job 改回旧命令。代码回退前增加一条检查：存在 `phase` 的未完成 job 就拒绝切旧版本。  
   **验收：** 准备一个 queued poll，只改配置不改代码，它应仍按 `not_before` 单次查询；再用旧代码镜像做一次干跑，启动器应在 exec 前拒绝，而不是把任务标成普通失败后重试。

10. **P2** Compose 默认机器额度从 1 CPU / 2G 抬到 14 CPU / 24G；每个 ffmpeg 的线程也多于计划里的「每进程 4」。  
   **触发：** 部署未设置 `EXACTCUT_CPUS` / `EXACTCUT_MEMORY` 时，compose 默认值生效。3 路 render 时，每个输入前还有 `-threads 1`，滤镜线程也各为 1，14 个 CPU 上实际线程数高于计划的 12。`pids_limit=512` 与计划一致。  
   **证据：** compose diff 约 398–403 行；`bounded_ffmpeg` 约 773–782 行；计划约 119、126 行。  
   **最小修正：** compose 默认值改回必须显式传入额度，避免静默升配。验收单里写上「每路 render 的线程上限」并在 `ps -T` 里核对。  
   **验收：** `docker inspect` 记录 CPU、内存、pids；3 路同时编码时，每个 ffmpeg 进程的线程数不超过约定值，宿主 load 与 RSS 有采样。

已覆盖、不必再担心的点：

- **同 batch 串行成立。** 准入用 `batch in batches`，数据来自本进程 owned 和仍活着的子进程，包含 `T` 状态；恢复时同一 batch 也只 `SIGCONT` 一个。无 batch 的旧任务按 `project` 互斥。证据约 1862–1863、1988–1993 行，以及 `test_same_batch_serial_including_stopped_executor`。
- **等待不占执行位，在阶段路径上成立。** poll 成功且仍为 `fbbm_queued` 时，把同一个 job 设回 `queued` 并写 `not_before`，去掉 pid/token，不 `sleep`；`not_before` 未到就 `continue`，别的 lane 可以启动。`ResourceDeferred` 以退出码 75 写 `waiting_resource`，释放预约和执行位。证据约 1609–1625、1656–1664、1981–1982、2391–2398 行。默认 `max_active_children=1` 时，同时运行的仍只有 1 个进程；A 在等 poll 时 B 可以占用这一个名额。这是相对旧 `sleep` 循环的行为变化，和「等待不占位」一致。
- **阶段链没有紧循环。** fetch 成功只创建一次 render（uuid5，文件已存在则返回）；render 不自动上传；upload 成功才创建 poll；poll 未完成按 `poll_seconds`（至少 1 秒）重入，`poll_started` 加 `wait_timeout_seconds` 超时后变为 `publish_failed`，人工 `queue_publish` 会清掉计时。证据约 1604–1636、2326–2388、2151 行。
- **活进程认领和安全重排成立。** 每 tick 都 `recover_jobs`，不看 owned 是否为空；进程表能对上 token 就认领。无阶段结果且没有 pending intent 时从本阶段重排；启动失败走 `local_failure` 标失败，SIGHUP/无结果走重排。证据约 1712–1758、1769–1791、2016–2022 行。
- **网页 upload/poll 的保险主路径成立。** 先落 intent 再调用；OSS 回读 SHA256；飞书回读 URL、素材 ID、重试次数、状态、视频 ID；回执先于核对落盘；守护模式下 CF 不睡眠、不循环重试。回读失败后第二次调用不再 ingest。旧 `workflow.publish` 复用了这套流程。证据约 994–1002、1044–1113、2281–2388、2960–3000 行附近的测试。
- **磁盘账本的归并、释放和重启重建成立。** 按 `st_dev` 把峰值加总后再跟空闲空间比较；自身预约不二次扣；失败、结束、推迟都会 `release`；重启按 admission 路径重映射设备号，死任务的预约被丢掉，暂停和仍活着的预览预约会保留。证据约 1416–1466、1473–1504 行及对应测试。
- **AI 令牌桶和「配额耗尽后不重复前半段」成立。** 跨进程 flock + 持久化令牌，时钟回拨不增发；耗尽抛 `ResourceDeferred`。镜头结论、Gemini 上传句柄、Hook 纠正标记、预检逐模型进度都会保住；文件查询不扣模型令牌；SDK 重试为 1 次。ffmpeg 在 `run_checked`、安全抽帧、Hook 抽帧三处都加了线程上限。证据：`ai_rate.py` 约 423–451 行，safety/hook/preflight diff 及 `test_step3_resources.py`。
- **schema 1 与新字段可选成立。** 未见 `schema_version` 改动。旧任务用 action 映射 lane：`publish`→upload，下载类→fetch，其余→render。`enabled=false` 时新提交仍走旧命令，并且任一运行中的进程会挡住下一个。

证据不足：

- 本审查只读了 `review-input.md`，没有跑测试，也没有对照仓库里 diff 以外的函数。`download()` 是否保证同集只下一份、`create_or_reuse`/`ingest` 如何处理非 200、`execution()` 有没有另一把 batch 锁、`worker()` 收割 `finish_job` 时是否持有 `LOCK`，材料里都看不到。
- 「所有 ffmpeg 都走 `bounded_ffmpeg`」只在 diff 出现的调用点上成立。
- `load_config` 对 default、production、runtime 的合并顺序只以实施记录为准。
- 生产验收表全部是「未执行」；471 个测试使用临时数据根和模拟客户端。3 路 render 的真实 RSS、线程和供应商配额没有测量数据。

---

# 第 2 轮复审（Grok，修复后）

日期：2026-10-10。审查对象：修复 diff `00e213f..8398466`。会话 `01a121c4`（见下）。
结论：第 1 轮 10 项中 9 项确认修复、第 10 项部分；新发现 5 个 P1，**不可进入生产验证**。

我先分段读完当前目录的 `review-input.md`，只依据材料对第 1 轮的 10 个问题逐条复审。第 1 轮 10 项里，9 项代码验收已成立，第 10 项只修了一部分。修复本身又引入了交付重放、核实卡死、失败回滚和证据缓存问题。按材料，现在不能进入生产真实验收。

材料只有 `review-input.md`（第 1 轮原文、`00e213f..8398466` 的 diff、实施记录）。未跑测试，也未看 diff 以外的函数体。下面的「已修复」只表示原触发条件和原文验收被 diff 里的代码和测试覆盖。

## 第 1 轮 10 项

**1. 暂停执行器不占 lane：已修复。**  
`resource_usage` 已去掉 `child_state != 'T'`，暂停进程按 `resident_identity` 计入 lane（`source/exactcut/webserver.py`，材料约 1666–1705 行）。`reconcile` 对所有非重复子进程先计数，再决定是否 `SIGCONT`（`source/exactcut/batch_control.py`，约 769–806 行）。`test_stopped_children_hold_lane_while_another_lane_can_start` 覆盖「暂停满 render 后同 lane 不得再启动、upload 仍可启动」（约 3553–3565 行）。恢复侧用 `counts > limit` 且不再次 `+= 1`，因为 T 状态已经在计数里；恰好满额可以继续，超过 lane 或总量则一个都不继续（约 3576–3583 行）。超额 T 进程不会自行退出，要人工清，这不破坏「第 4 个 render 不得启动」。RSS 仍只出现在未执行的生产操作单里。

**2. 全局锁内对整集做 SHA256：已修复。**  
调度改走 `scheduler_fingerprint` / `scheduler_verified`：快路径只核对 dev/ino/size/mtime_ns/ctime_ns 和已落盘摘要；旧结果最多两个后台任务，调用方收到 `EvidencePending` 即返回（`source/exactcut/stage_results.py`，约 1004–1143 行）。`worker_tick` 在指纹和预算两处捕获 `EvidencePending` 后继续调度其他任务（`webserver.py`，约 1778–1797 行）。HTTP 确认把 `approval_evidence` 放在两段 `mutation_lock()+LOCK` 中间（约 1868–1874 行）。`test_scheduler_legacy_one_gib_hash_does_not_block_other_user` 要求哈希线程不持两把全局锁，且调度 tick 自身在哈希结束前返回（约 2602–2639 行）。`test_http_approval_hash_releases_global_lock_for_other_user` 要求另一用户在 1 秒内得到 202（约 2344–2374 行）。这是受控慢摘要，不是真实磁盘吞吐。

**3. completed 且校验失败时既不建后继也不改状态：已修复。**  
`recover_jobs` 在 completed fetch/upload、有 attempt、没有确定性后继、且 `verify` 失败时写入「阶段结果缺失或校验失败…」，并 `finish_job(..., local_failure=True)`（约 1443–1452 行）。缺结果或不可信产物不会停在 `completed`。交付意图仍待核实时落到 `unverified`，不建后继（`test_completed_missing_result_does_not_stick`，约 2586–2599 行；`test_completed_invalid_artifact_has_visible_failure`，约 2848–2860 行）。无 attempt 仍会 `advance_stage`，这与原文触发条件（带 attempt）一致。

**4. 待核实没有放行入口，且守护模式不再重登：已修复（主路径）。**  
管理员 `POST .../verify-delivery` 只收 `request_id`、成片 SHA、可选 token（约 1831–1894 行）。`prepare_staged_job` 在读 `enabled` 之前把该动作固定为 poll（约 1298–1302 行）。`verify_record_once` 只读飞书记录和 `sync_status`（`publish.py`，约 899–916 行）。`queue_publish` / `upload` / `poll` 在存在 `verification_only` 或 `verification_history` 时拒绝上传和普通同步（`workflow.py`，约 1992–1993、2055–2056、2080–2083 行）。守护模式 `attempts=2`，100006/100009 会 `login()` 一次（约 868–893 行）。超时后再核实，ingest/OSS/飞书写均为 0（约 2230–2246 行）；本地启动失败后仍只能 poll 原回执（约 2517–2533 行）。重登被接到了所有 CF 请求上，由此带来的新问题见下方第 2 条。

**5. 独立 CLI `execute --publish` 仍是旧同步发布：已修复。**  
`cli._main` 在 `load_env_file`、读配置和编译之前直接抛错（`cli.py`，约 814–816 行）。带 `--publish-dry-run` 同样拒绝；单独干跑仍可用（约 2205–2212、2414–2418 行）。这满足原文验收的后半段：非 0 退出且没有外部写。

**6. 缺总量时默认不是 1，且生产模板不会作用于现网：已修复。**  
`settings()` 在缺少 `max_active_children` 时使用 1（`concurrency.py`，约 836–837 行）。`main` 在 `claim_data_root` 之前打出 `Effective concurrency settings`（约 1944–1946 行）。三种配置（缺整段、只有 lane、完整段含总量 8）由 `test_missing_total_is_conservative_without_default_merge` 和 `test_startup_logs_merged_effective_limits_before_initializing_data` 覆盖（约 3595–3618 行）。现网 runtime 仍不会被模板自动覆盖；操作单要求先对 hash 和生效日志，不一致则停止放量（约 322–378 行）。原文「现网缺段必然总量 4」与材料中的 `load_config` 合并说明不一致，函数级回退本身已改为 1。

**7. upload/poll 不占磁盘，同盘 render 按三份相加：已修复（同盘验收）。**  
`render_allocations` 把 project、输出、TMPDIR 分成 `budget//4`、`budget//4`、余量，同路径相加后仍是一份 `render_budget`（`storage.py`，约 1157–1167 行）。`prepare_render` 先按 `st_dev` 归并再 `require_space`（约 1174–1177 行）。upload/poll 以及旧 `publish` action 按成片 `st_size` 预约（约 1219–1222、2794–2797 行）。同盘测试要求一份预算可预约、第二份拒绝（约 2657–2669、2642–2654 行）。分盘时输出目录只拿到四分之一，这是新问题，见下方第 1 条。

**8. poll 容量、恢复顺序、排队可见性：已修复。**  
`production.json` 的 poll 从 1 改为 4，总量仍是 8（约 510–515 行）。`test_production_allows_four_polls_with_render_within_total` 要求 4 个 poll 加 1 个 render 同时进入 owned（约 3621–3631 行）。`reconcile` 与准入共用 `owner_round_robin` 和 `scheduler.json`（约 787–805、1623–1653 行）。恢复顺序测试在游标为 alice 时先继续 bob（pid 103），游标为 bob 时先继续 alice（约 3634–3646 行）。`queue_position` 只在响应里按公平遍历现算，不写回 job，客户端也不能指定（约 1656–1663、3649–3700 行）。

**9. 关开关或退回旧代码不能把已入队阶段任务变回旧命令：已修复。**  
`enabled=false` 不改写已落盘的 poll 命令和 `not_before`，到期后仍只查一次（约 3190–3199 行；文档约 266–267 行）。旧镜像没有 `io.exactcut.stage-protocol=1` 时，`phase_blockers` 在 `engine.apply` 之前拒绝，queued、暂停、unverified、坏 JSON、以及终态 job 上仍为 unverified/pending 的交付都会挡住（`linux_ops.py`，约 575–663、3342–3377 行）。`check-switch` 只读，失败退出码 1（约 605–623、3427–3439 行）。停服后、健康失败回滚前都会再查。Mac launchctl 管理器写明不在本轮美东路径。守卫过宽和回滚停服见下方第 3 条。

**10. Compose 默认 14C/24G，ffmpeg 线程多于「每进程 4」：部分修复。**  
dev compose 已限制为 1 CPU / 2g / `pids_limit` 256（`docker-compose.yml`，约 719–721 行）。`validate_resources` 在 `Docker.compose`、初始化和发布路径上拒绝缺失或非法额度，并用 `deployment.json` 覆盖继承环境（约 534–548、566–570、3483–3502 行）。生产 compose 默认仍是 `EXACTCUT_CPUS:-14.0`、`EXACTCUT_MEMORY:-24g`、`pids_limit: 512`（测试断言约 3474–3480 行）。直接跑 compose、不经过管理器时，静默 14/24 仍在。ffmpeg 没有进程级线程封顶；实施记录写明 `-threads 4` 只约束编码器，NLWP 不保证为 4，操作单只采样、没有数值门闩（约 235、380–393 行）。原文验收「每个 ffmpeg 不超过约定值」还没有约定值。保持该实例 14/24 是材料里的明确决定，不能算漏改。

## 新的 P0 / P1

未发现新的 P0（材料里没有稳定双开同一 batch，也没有 poll 空转占死执行位的新路径）。有 5 个新的 P1。

**P1-1. 分盘时 render 输出盘按四分之一预算放行。**  
触发：project、成片目录、`TMPDIR` 的 `st_dev` 不同，编码输出接近原来的整份 `render_budget`。  
证据：`render_allocations` 把输出目录写成 `budget // 4`，TMPDIR 拿一半（约 1163–1166 行）。`device_allocations` 只把同一设备上的份额相加，所以分盘后输出设备的门槛是总额的四分之一。原「不同文件系统」测试被改成断言同盘只检查一次且字节数等于整份 budget（约 2166–2171 行），分盘不再被覆盖。  
最小修正：输出设备按整份输出峰值预约；临时峰值单独预约，并在注释中写明这两份与 `render_budget()` 的对应关系。同盘若峰值不重叠，预约 `max(输出, 临时)`，不要把输出峰值改成总额的四分之一。补一个三设备测试：输出盘剩余只够 `budget//4` 时必须拒绝。  
验收：三套 `st_dev` 上，输出盘剩余小于一份输出峰值时 render 不得启动；同盘剩余等于一份输出峰值加一份临时峰值时可以启动第二份必须拒绝。

**P1-2. 守护模式把同一次 CF 请求再发一遍，写接口可能双创。**  
触发：受 `guarded_delivery` 保护的调用（含仍会写的 `sync_record_once`）收到 JSON `code` 100006 或 100009，且 `login()` 成功。  
证据：`attempts = 2`，`attempt == 0` 时清空 token、`login()`、`continue`，循环用同一 `method/path/**kwargs` 再请求（约 868–893 行）。非 200 且非这两码才在守护模式下直接返回。测试把这个双发固定为 `request.call_count == 2`（约 2191–2201 行）。材料没有证明 CF 返回 100006 时写操作尚未落账。  
最小修正：100006/100009 只重新登录一次；重试仅限已标明幂等的读（核实路径的 `get_record` / `sync_status`）。ingest、resync、创建、更新得到这两码时进入待核实，不再发第二次。  
验收：对写接口注入「服务端已落账但响应 100006」，外部创建次数仍为 1，job 为 `unverified`；对核实读接口仍能看到一次 login 和一次成功的原记录查询。

**P1-3. 核实先改 publications，job 没落下时两条入口都拒绝。**  
触发：`queue_verify_delivery` 已写盘，随后 `workflow.detail`、`save` 或进程被杀，job 没有进入 `JOBS`。  
证据：`queue_verify_delivery` 在返回命令前把 history、`verification_only`、`status='fbbm_queued'`、`intent.status='verified'` 写入 publications（约 2036–2043 行）。HTTP 在这之后才组装 job 并 `save`（约 1884–1893 行）。异常分支只 `respond(400)`（约 1895–1896 行），没有回滚 publication。此后 `queue_verify_delivery` 要求 `delivery_blocked`（约 2033 行）；主路径测试里这次写入之后 poll 能继续，说明该状态已经不再算待核实阻塞。`queue_publish` 见到 history 即拒绝（约 1992–1993 行）。  
最小修正：先写 queued job（或同一文件事务里先写「待执行核实」），job 落盘成功后再改 publication；失败则恢复原 unverified/pending。启动扫描要能认出「history 已写、核实 job 不存在」并恢复为可再次核实。  
验收：在 `publications.json` 写入之后、`save(job)` 之前注入异常。重试同一核实必须得到 202，且全程 ingest/OSS/飞书创建次数为 0；普通上传仍被拒绝。

**P1-4. 新版本健康检查失败时，自动回到旧镜像会被守卫打断，服务可以停在已停服状态。**  
触发：现网已有 `unverified` 或 intent `pending` 的阶段 job（它们不在 `ACTIVE` 里）。正向发布允许切到带协议标签的新镜像；新容器启动后健康检查失败，回滚要 `apply` 旧镜像。  
证据：`ACTIVE` 只有 queued/downloading/indexing/producing/publishing/running（约 525 行），`await_idle` 只看这个集合（约 626–628 行）。`require_stage_compatibility` 仅在目标镜像不支持阶段协议且存在 `phase_blockers` 时抛错（约 600–603 行），因此新镜像正向切换不看待核实。失败分支先停掉新容器，再对旧镜像做同一检查，通过后才 `apply`（约 658–662 行）。另外 `phase_blockers` 对已完成且带 `batch_id`+`request_id` 的 job 无条件 `read(publications.json)`，缺文件或坏 JSON 都算阻断（约 584–591 行）。审片等待中的已完成 render/fetch 往往还没有这份文件，紧急回退会被误拒。  
最小修正：正向发布同样要求 `phase_blockers` 为空，或健康失败时允许回到「刚刚停掉的旧版本」并保持维护锁，直到待核实被显式处置。`publications.json` 不存在应视为没有待核实交付，不要当成阻断；坏 JSON 继续失败关闭。  
验收：准备一条已完成 render、无 publications 文件，以及一条 unverified。前者 `check-switch` 旧镜像应允许（在队列已空时）；后者正向发布应在停服前拒绝。人为让新版本 health 失败时，容器必须回到旧 release，或停在维护态并给出可执行的恢复命令，不能既停新容器又不拉起旧容器。

**P1-5. 证据任务失败被缓存，同一次进程内不再重算；池子也没有超时。**  
触发：旧结果的强校验在池里碰到一次 `ValueError`（读盘瞬断、摘要过程中文件身份变化），或两个 `file_sha256` 一直不返回。  
证据：`background()` 把已完成 future 放进 `_READY`，再次调用直接 `future.result()`（约 1051–1064 行）。失败 future 不会从 `_READY` 删掉。`scheduler_verified` 把 `ValueError` 等吞成 `None`（约 1142–1143 行）。completed fetch/upload 看到 `None` 就永久 `finish_job` 失败（约 1446–1451 行），重启也不会把已写成 failed 的 job 改回重算。池子 `max_workers=2`，满员只抛 `EvidencePending`（约 1011、1062–1064 行），没有截止时间。同 batch 在 `EvidencePending` 时被放进 `evidence_waiting`（约 1608–1611、1770–1771 行），摘要不返回就一直不放行。  
最小修正：失败 future 不得进入可复用缓存；`None` 与异常分开，瞬断应在下一 tick 重算，只有身份不符才把 job 标失败。给单次摘要加超时，超时释放槽位并只延迟该 job。  
验收：注入一次随后消失的读错误，completed fetch 在下一 tick 仍能推进 render。占满两个槽并让它们阻塞时，第三个 batch 的非证据任务仍能启动；超时后原槽位必须能接新任务。

## 结论

结论：不可进入生产真实验收；必须先解决这四项阻断：守护模式对 100006/100009 重放可能写两次的 CF 请求、核实 publication 与 job 非原子导致放行和上传都拒绝、新版本健康检查失败后无法自动恢复旧镜像（含缺 publications 文件时的误阻断）、证据失败被缓存或两个摘要槽卡死导致阶段被永久判失败或同批不再推进。

---

# 第 3 轮复审（Grok，最终）

日期：2026-10-10。审查对象：修复 diff `8398466..3dd0ca8`。会话 `01a1222c-6bac-7102-b583-aaea573bf6f3`。
结论：第 2 轮 5 个 P1 全部确认修复；新发现 2 个 P1（P1-A waiting_resource 误回滚、P1-B 首次升级健康失败恢复路径），**暂不可进入生产验证**。
编排者独立复核：全量 pytest exit=0（多次），但 `test_preview_deferral_preserves_verified_partial_cache_for_retry` 偶发失败（4 次全量中 1 次），属测试确定性 P2。

我先分段读完 `review-input.md`，只依据材料判定第 2 轮 5 个 P1 是否修好，并给出能否进入生产真实验收的结论。材料前半段已读完，接着读修复 diff 的实现和测试，按 5 个 P1 对证据。第 2 轮 5 个 P1 的原触发条件，在 `8398466..3dd0ca8` 的 diff 里都有对得上的代码和测试。未发现新的 P0。有 2 个新的 P1，当前不能进入生产真实验收。判断只依据 `review-input.md`，没有跑测试。

## 第 2 轮 5 个 P1

**P1-1 分盘输出预算：已修复。**  
`render_allocations` 把输出目录写成完整 `budget`（即一份 `render_budget()`），project 为 `budget // 4`，`TMPDIR` 为 `budget - budget // 4`；注释写明输出峰值与临时峰值可以并存，同盘相加为两份。`source/exactcut/storage.py` 的该函数即如此。`test_three_disks_reject_quarter_output_peak` 在三套 `st_dev` 上断言 `paths[output] == 1000`，输出盘 `free=260`、`reserve_bytes=10` 时 `reserve` 失败（旧的 `budget//4` 门槛是 250+10，这个空闲量会被放行）。`test_same_disk_reserves_full_output_plus_independent_temporary` 断言合计 2000、`free=2010` 时第一份成功、第二份拒绝、释放后可再成功。原同盘测试改为断言 `2 * budget`，`prepare_render` 的检查次数仍为 1、字节数为 `2 * expected`。

**P1-2 守护写请求重放：已修复。**  
`CfAdsClient.request` 只把守护模式下的 `GET /api/v1/facebook/app_usage` 和 `GET /api/v1/material/…/sync_status` 标为 `safe_read`（`attempts=2`，且仅此时对 100006/100009 清 token 并 `login()` 再发一次）。守护写请求 `attempts=1`，条件 `not guarded or safe_read` 不成立，不登录、不重放。`test_guarded_write_auth_response_never_replays` 覆盖 ingest、resync、PUT、DELETE 两种过期码，`call_count == 1` 且 `login` 未调用。`test_ingest_committed_but_auth_response_leaves_job_unverified` 走真实 `poll`：外部创建 1 次，job 为 `unverified`，intent 为 `pending`。`test_guarded_material_read_relogs_once` 与改写后的 header 测试仍要求核实读一次 login、两次请求。

**P1-3 核实非原子：已修复。**  
`queue_verify_delivery` 在改 history / `verification_only` / `fbbm_queued` 之前，把预分配 `job_id` 和 `previous` 快照写入同一份 `verification_queue`。HTTP 先定 `job_id`，`save` 成功后调用 `recover_verification_queue` 弹出该标记；异常路径和 `recover_jobs` 开头走同一函数。无匹配 job 时事务恢复 `previous`；旧交接（含已被扫成 `publish_failed`、但仍带 `verification_only` 且 intent phase 为 verification）恢复为 `unverified` / intent `pending`。`test_publication_handoff_failure_can_retry_same_verification` 在 publication 已是 `fbbm_queued` 之后、`save` 或 `detail` 处注入异常，得到 400，状态回到 `unverified`，再次提交为 202，`queue_publish` 拒绝，poll 的 ingest / OSS / 飞书创建为 0。重启孤儿与「保存后响应丢失」两条测试分别覆盖回滚和已落盘 job 保留。

**P1-4 回退守卫 / 健康失败恢复：已修复。**  
`check_switch` 的 `allowed` 改为「没有 active task 且没有 phase_blockers」，不再因目标镜像带阶段协议而放行。`require_phase_drained` 加在停服前的多次检查上。健康失败分支去掉对旧镜像的 `require_stage_compatibility`，改为：若此时已有 `phase_blockers` 则留下维护锁，然后 `apply` 刚刚停掉的那份旧 release。维护保留时抛出带绝对路径的 `linux-ops … recover`。`test_forward_release_refuses_uncertain_delivery_before_stop` 断言 `engine.calls == []`、版本仍是 old、没有维护文件。`test_health_failure_with_post_stop_uncertainty_restores_old_under_maintenance` 断言 `apply/health old`、`current.json` 回到 old、维护文件存在。无 publications 的已完成 fetch/render 允许切换；坏 JSON（`{`、`[]`、`null`）仍然拒绝。材料说明缺文件原本就是 `read(..., default=None)` 得到 `{}`，不构成阻断。

**P1-5 证据失败缓存与超时：已修复。**  
线程池换成最多两个 spawn 进程。`background` 只把 `result()` 的成功值放进 `_READY`，异常不入缓存，只回给同一个 key。`scheduler_verified` 把 `EvidenceMismatch`、坏 JSON、缺字段收成 `None`；`OSError` / `ValueError` / `RuntimeError` 变成 `EvidencePending`。`verified(..., _retry_errors=True)` 让瞬断抛出而不是变成 `None`。父进程 `Timer` 与子进程 `SIGALRM` 都会杀掉独立进程组，默认 120 秒，可用 `concurrency.evidence_timeout_seconds` 覆盖。`test_failed_future_is_not_reusable_cache` 要求失败后下一次重算成功。`test_completed_fetch_retries_transient_read_next_tick` 对 `ValueError` 和 `OSError`：下一 tick 之前保持 `completed` 且没有 render，错误消失后恰好出现一个 render。`test_two_blocked_readers_timeout_and_reclaim_real_slots` 要求两个槽阻塞时另一个 batch 的 upload 仍能进 owned，超时后原 PID 消失且新读取能占到槽。

## 新的 P0 / P1

未发现新的 P0。有 2 个新的 P1。

**P1-A. 核实恢复把仍会再跑的 `waiting_resource` 任务当成孤儿。**  
`recover_verification_queue` 只把 job 状态 `queued` / `running` / `completed` 视为已落盘。提交后 `verification_queue` 会被弹出，publication 仍是 `fbbm_queued` + `verification_only` + intent phase `verification`，于是每 tick 都走 legacy 分支。磁盘不足时 poll 以退出码 75 写成 `waiting_resource`（第 2 轮审查已写明这个状态），该 job 匹配失败，函数把 publication 改回 `unverified`、intent `pending`，并 `update(..., 'interrupted', ...)`。任务文件还在，之后仍会再执行 poll；此时间接又允许再提交一次核实。这不是「job 没写上」的原窗口，原窗口的 202 重试测试盖不住它。

**P1-B. 首次从现网旧镜像升级时，健康失败且出现待核实，文档里的恢复步骤走不完。**  
正向切换在停服前就要求 `phase_blockers` 为空。健康失败且此时已有阻断时，代码会拉起刚刚停掉的旧 release 并保持维护锁，提示先核实再执行 `linux-ops recover`。核实入口是本轮应用内的 `verify-delivery`。材料写明这次尚未部署，现网旧镜像没有这个入口；同时又是先启动 worker，再等健康检查。健康等待期间新 worker 可以写出 `unverified`。此后旧容器在跑，但没有核实 API；再次 `transition` 会被 `require_phase_drained` 拒绝。无阻断的健康失败路径会恢复旧版本并撤维护，那条不受这一项影响。

结论：不可进入生产真实验收；阻断项为 P1-A（`waiting_resource` 的存活核实任务会被交接恢复误回滚并打断 batch）和 P1-B（旧镜像健康回退后无法按运行手册排空待核实，且不能再正向发布）。
