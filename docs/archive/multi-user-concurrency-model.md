# 多用户多任务处理模型（架构提案）

> 历史归档（2026-11-04）。正文保留当时的方案或执行状态，不代表当前版本；日常操作见[文档入口](../README.md)。

日期：2026-10-09。定位：在现有单机 Python 应用上，给出一套**稳健、可实现、可恢复**的多用户多任务模型。
约束：单机、单数据根、持久 JSON、无外部消息中间件。
生产容量（美东实例，见 [服务器规格](server-sizing.md)）：宿主 16 vCPU / 约 30 GiB；应用容器 `cpus=14 / mem=24GiB`；当前仍串行执行。

> **文档状态**：本文件是分析阶段的完整模型。**实施规范以 [并发改造计划](concurrency-refactor-plan.md) 为准**；两者冲突时以计划为准。
> **业务前提更正**：实际业务中多用户通常**不会剪同一部剧**，因此本文件中"跨用户共享下载 / 共享基底"的复杂协议**可整体后移**，改造计划已据此简化。

---

## 0. 结论先行

1. **不要引入 Redis/Celery**。规模是"单机 + 少量用户 + 突发任务"，现有 `ThreadingHTTPServer + 单 worker + JSON 持久化` 的骨架是对的，问题不在缺中间件。
2. 真正要改的是**三个结构性错误**：
   - **执行位被"等待"占用**（FBBM 轮询、下载、AI 调用都挂在子进程里，而全局只有 1 个执行位）。
   - **并发不是按资源类型分池，而是全局一个数字**。
   - **任务状态是"进程活着/死了"的二元判断，没有阶段化可恢复的结果契约**。
3. 目标模型 = **按资源类型分池 + 阶段化 Job + 共享工作注册表 + 租约/预留**。
4. 第一原则：**等待（外部 API、网络、依赖）绝不占用执行位**；只有真正消耗 CPU / 带宽 / 磁盘写的阶段才进入池。
5. 并发上限**由资源预算推导**：`render` 池由 CPU/内存决定，`download`/`upload` 由带宽决定，`external` 由配额决定。不是"2 还是 4"的拍脑袋。

---

## 1. 现状判断（已按代码核实）

| 机制 | 位置 | 对并发的含义 |
| --- | --- | --- |
| 单 worker，`if running: return` + 循环末尾 `break` | `webserver.py:307,345` | 全局同时只有 1 个子进程 |
| `reconcile` 有任何非暂停子进程即 busy，恢复只 `SIGCONT` 一个 | `batch_control.py:250-283` | 恢复逻辑天生串行 |
| `_publish` 上传后 `while: sync(); sleep(poll)` | `workflow.py:754-811` | **FBBM 等待独占执行位** |
| 旧版 `execute --publish --wait-ready` 在渲染进程内等 | `orchestrator.py:120-190` | 存在第二条单体发布路径 |
| `episode_lock` 阻塞 `LOCK_EX`，命中缓存也先拿独占锁 | `source_cache.py:48-62,263-266` | 同集并发会堵死槽位 |
| 基底缓存 `LOCK_EX` 覆盖抽帧 + Hook 叠加 | `renderer.py:209-265` | 同基底不同 Hook 完全串行 |
| `require_space` 只看当下 free | `storage.py:118-127` | 并发下磁盘额度 TOCTOU |
| OSS 先查后传、飞书先搜后建 | `publish.py:92-115,187-205` | 并发写竞态 |
| ffmpeg 无 `-threads` 限制 | 全仓库无命中 | 每个编码按可见核数开线程 |
| 进程身份 = `process_token` + `instance_marker` + `ps` 表 | `batch_control.py:88-115` | 可复用，是恢复模型的基础 |
| 暂停 = `SIGSTOP` 进程组 | `batch_control.py:159-175` | 会冻结锁持有者 |

**结论**：现有代码已经有很好的"可恢复"原语（token 身份、原子写、原子发布、缓存复用），缺的是**阶段化调度**和**资源分池**。

---

## 2. 设计原则

1. **等待 ≠ 执行**：任何"等外部结果 / 等依赖 / 等资源"的状态不占用执行位，只占一条有界的等待记录。
2. **阶段化 + 幂等 + 原子发布**：每个阶段产出不可变结果（含输入指纹 + sha256），重启后可从上个未完成阶段续跑。
3. **单一写者 + 内容寻址共享**：共享素材（正片、基底、AI 结果）由唯一写者生成，读者在共享锁内核验后复用。
4. **按资源类型分池，按预算准入**：CPU 密集 / 网络 IO / 外部 API 分别限额，互不挤占。
5. **协作式暂停**：不跨租户 `SIGSTOP`；只在安全点暂停私有工作，共享写者不受单个用户暂停影响。
6. **外部写 = intent → write → reconcile**：写前落盘意图，写后核对，绝不做无依据的重放。
7. **公平按 owner，隔离按 batch**：`accounts.owner(batch)` 是资源归属；共享缓存只复用字节，不继承任务状态与发布权限。

---

## 3. 核心抽象

```
Task(batch 生命周期)
  └── Phase DAG: fetch → index → render(×N requests) → preview → confirm → upload → poll(FBBM)
        └── Job: 一个可独立调度、可恢复的持久步骤
SharedWork: 与任何 batch 解耦的公共写工作（按 drama+episode / cache-key 唯一）
Lease: 进程所有权租约（token + pid/pgid + instance + 心跳 + fencing epoch）
Pool: 按资源分类的执行槽（render / download / upload / external / preview）
Reservation: 磁盘与资源预留账本（按 st_dev 归并，峰值预算，已落盘冲抵）
```

- **Task**：现有 `runtime.json` 的角色，但状态由 **Phase 结果派生**，而不是由子进程存活推导。
- **Job**：现有 `.web-state/jobs/*.json` 的角色。新增字段：`phase`、`depends_on`、`pool`、`next_run_at`、`lease`、`result_ref`、`attempt`。
- **SharedWork**：新增。解决"多个 batch 需要同一集正片 / 同一基底"——目前这段逻辑散在 `source_cache` 和 `renderer` 里，且挂在调用者进程下。
- **Lease**：复用现有 `process_token` + `instance_marker`，增加**心跳**与**fencing epoch**，用于安全接管与防止过期写者。
- **Reservation**：替换 `require_space` 的 TOCTOU。

---

## 4. Phase 契约（可恢复性的地基）

每个 Job 完成后写一份**不可变阶段结果**，调度器据此推进，而不是"看进程退出码"：

```json
{
  "phase": "render",
  "batch_id": "...", "request_id": "...",
  "input_fingerprint": "sha256(...)",
  "status": "done | waiting_dep | waiting_external | waiting_resource | failed | unverified",
  "outputs": {"master": "production/videos/x.mp4", "sha256": "..."},
  "pool": "render", "attempt": 2, "ended_at": "..."
}
```

| Phase | 池 | 幂等/恢复方式 | 结果 |
| --- | --- | --- | --- |
| `fetch` | download | 按内容校验后复用；`.part` 原子替换 | 正片就绪（SharedWork） |
| `index` | render(短) | 按源摘要 + 版本键复用 | 镜头索引 |
| `render` | render | 基底按 key 复用；成片按 fingerprint 复用 | 成片 + sha256 |
| `preview` | preview | 按成片 sha 复用 | 低清预览 |
| `upload` | upload | OSS 按 key + 元数据幂等；先落意图 | OSS object |
| `poll` | external | 单次 `sync_record_once`；按记录互斥、按时间调度 | FBBM 状态 |
| `ai` | external | 按输入摘要 + 模型 + 版本缓存 | 强调/避让结果 |

**判定业务完成，只看 Phase 结果**。子进程退出码只表示"当前阶段进程结束"。

---

## 5. 调度器算法（单线程、事件驱动）

调度器**从不阻塞**在媒体计算、网络或文件锁上。

```
on event (enqueue | job_done | timer | user_action | restart):
    reap_finished_leases()                 # 每次 tick 收割全部，不依赖 owned 为空
    advance_tasks_from_phase_results()     # 由结果派生 Task 状态

    ready = jobs where deps_done and not waiting and next_run_at <= now
    order(ready):                          # 公平：按 owner 轮转，owner 内 FIFO
        1) unblock_small   # 能解锁其他用户小任务的优先（避免队头阻塞）
        2) fair_share      # 轮流给每个 owner 一次机会
        3) fifo

    for job in ordered(ready):
        按 job.pool 取空闲容量
        若无容量：continue（不是 return！）  # 一个池满不挡别的池
        申请 reservation（磁盘/内存）
        持久化 admission + lease，然后 spawn
        继续尝试下一个 job，直到所有池满或 ready 耗尽
```

要点：
- **`continue` 而不是 `return`**：一个池满不能挡住别的池，这是当前 `if running: return` 的直接修复。
- **等待进入定时器**：`poll`/`waiting_dep` 用 `next_run_at` 放回 ready，不占槽。
- **admission 先落盘再 spawn**：崩溃后可重建。

---

## 6. 资源池与容量

| 池 | 资源 | 容量推导 | 备注 |
| --- | --- | --- | --- |
| `render` | CPU + 内存 | `min(CPU预算/单编码成本, 内存/峰值, 硬上限)` | 唯一 CPU 密集池；14 CPU → 初始 3（每任务 `-threads 4`） |
| `preview` | CPU（短） | 复用 render 预算或独立 1 | 现有 `HOOK_PREVIEWS=BoundedSemaphore(2)` |
| `download` | 网络带宽 | 由实测吞吐（≈4.5 MiB/s）与磁盘写决定 | 同集去重，只 1 个实际写者 |
| `upload` | 网络 + OSS 配额 | 小 | 与 render 独立 |
| `external` | 外部 API 配额 + 令牌桶 | 全局令牌桶 + 每记录互斥 | AI / Feishu / Meta 共用 |
| `paused` | 内存 | 常驻暂停 ≤ N，且总量含运行中 | SIGSTOP 不释放内存 |

**容量不是常量，是配置 + 实测校准**：
- 生产为 14 CPU / 24 GiB：`render` 初始 3（每个 ffmpeg `-threads 4`，合计 12 线程，不超订）。
- 即使容量设为 1，收益仍来自 **IO 池与 CPU 池分离**（A 下载/等 FBBM 时 B 能渲染）。
- 所有池的**活动进程总数**受 `pids_limit`（需从 256 调高到 ≥512）与内存双重约束。
- 必须给每个 ffmpeg 加 `-threads` 上限，否则多个任务会抢满所有核。

**每个池的准入都要先过 reservation**：磁盘按 `st_dev` 归并、按峰值预留、已落盘冲抵、失败释放。

---

## 7. 可恢复性（崩溃/重启语义）

**目标语义：阶段边界可续跑；外部写不重复、不丢失。**

重启恢复算法：

```
recover():
    leases = load_persisted_leases()
    for lease in leases:
        if live(lease.process_token, instance, pid/pgid):   # 复用现有 child_state
            adopt(lease, heartbeat=fresh)
        else:
            requeue_last_incomplete_phase(lease.job)        # 幂等：先校验已有产出
    rebuild_pools_and_reservations()
    reschedule()
```

- **进程存活 → 接管**（复用现有 `process_token` + `instance_marker` + `ps` 校验，含 `SIGSTOP` 的 `T` 进程）。
- **进程死亡 → 从最后未完成阶段重排**：因为每阶段幂等（render 复基底、upload 核 OSS、poll 复用记录），重排是安全的。
- **外部写窗口**：`upload`/`poll` 先落 **intent 记录**（目标对象、已批准 sha、记录 id 或"待核实"），写后 reconcile。结果不明 → `unverified`，**绝不盲重放**。
- **FBBM 等待是正常态**：无进程的 `waiting_external` 不判 `interrupted`（修复现有 `finish_job`/`recover_jobs` 的误判）。
- **临时文件**：`.part`、staging 目录在恢复时于独占锁内清理，不依赖 `finally`。

---

## 8. 暂停 / 取消 / 归档（协作式）

- **暂停** = Task 置 `paused`：调度器不再为其准入新 Job；**私有**运行中 Job 收到"安全点停止"请求，在**不持 runtime/共享独占锁、不持 AI 许可**处自行停止并持久确认。
- **共享写者不响应单个用户的暂停**：A 暂停不影响 B 依赖的同集下载/基底。这正是消除"暂停卡住别人"的关键。
- **不使用跨租户 `SIGSTOP`**：如果保留 SIGSTOP，只用于私有、且**锁范围已缩小到不含共享独占锁**的进程。
- **取消/归档/删除**：只移除该 Task 的订阅；无订阅的 SharedWork 由调度器核验身份后 `SIGTERM` 并清理。
- **恢复**：按同一套容量检查重新准入；暂停/归档仍计入 owner 未完成额度，防止绕过限额。

---

## 9. 多用户隔离与共享

| 维度 | 隔离 | 共享 |
| --- | --- | --- |
| 草稿/确认/时间线/成片归属 | ✅ 按 batch | ❌ |
| 任务状态/进度/错误/日志 | ✅ 按 batch | ❌ |
| 正片/基底/AI 计算结果 | 内容寻址，读者共享 | ✅ 同实例、同素材 |
| 发布记录 / FBBM 映射 / 上传权限 | ✅ 按 batch | ❌ 字节相同也不继承 |
| 磁盘/CPU/带宽/API 配额 | 全局预算 + 按 owner 公平 | 共享 |

- 资源归属 = `accounts.owner(batch)`（`access.json`），旧项目回退 admin；`actor` 仅审计。
- 共享缓存**只按内容与版本匹配**，不按剧号；回收以非阻塞独占锁为准，避开活跃读者/写者。

---

## 10. 与现有代码的映射（改哪里）

| 目标 | 现状 | 改动 |
| --- | --- | --- |
| 多池准入 | `worker_tick` 单进程 + `if running: return` | 引入 Pool/容量表；循环改 `continue`；admission 先落盘 |
| 等待不占位 | `_publish` 内轮询；`wait_until_ready` | 拆 `upload` / `poll` 两个 Job，`poll` 由 `next_run_at` 调度 |
| 单一发布路径 | 新版 `_publish` + 旧版 `execute --publish` | 旧路径降级为兼容适配器，新任务只走阶段化 Job |
| 共享下载 | `source_cache.ensure` 挂在 batch 进程 | 提取 SharedWork 注册表 + 订阅集合；命中改非阻塞共享锁 |
| 共享基底 | `renderer` 独占锁覆盖 Hook | 独占段仅"检查/生成/替换/meta"，Hook 在段外；未命中返回依赖等待 |
| 磁盘预留 | `require_space` 只看 free | 新增 Reservation 账本（按 `st_dev`、峰值、冲抵、恢复） |
| 暂停 | `SIGSTOP` + `reconcile` | 安全点协作暂停；共享写者不响应；恢复按池容量 |
| 恢复 | `finish_job`/`recover_jobs` 二元判定 | Phase 结果契约 + 每 tick 全量收割 + `unverified` 态 |
| 外部幂等 | 先查后写 | intent → write → reconcile；多匹配/归属不明 → 待核实 |
| 资源预算 | 无 | `render/download/upload/external/paused` 五池 + 全局活动上限 |

---

## 11. 分阶段落地（强烈建议按此顺序）

**阶段 A｜消除"等待占位"（最高性价比，先上线）**
- `render` 池容量 2（或先 1）+ **IO/等待移出执行位**：FBBM 等待、下载、AI 调用独立成短 Job。
- 调度循环 `continue` 化 + 共享锁非阻塞化（`episode_lock` 命中改 `LOCK_SH`）。
- 收益：A 等 FBBM 时 B 能渲染；不动共享归属协议。

**阶段 B｜阶段化 + 可恢复 + 分池**
- 引入 Phase 结果契约、Pool、Reservation、Lease 心跳。
- 磁盘预留、每 tick 全量收割、`unverified` 外部结果态。
- 收益：崩溃后可续跑；并发安全。

**阶段 C｜共享工作与外部幂等加固**
- SharedWork 注册表（下载/基底订阅、取消、接管）。
- 外部写 intent/reconcile、多匹配处理、命名碰撞。
- 收益：多用户同素材并发正确、跨实例语义清晰。

**明确后移**：媒体探测持久缓存、抽样帧/缩略图缓存、整条成片/跨项目 QC 缓存、镜头索引并发生成、空闲额度借用。

**验收矩阵**（每阶段）：A 等 FBBM 时 B 制作/上传；两用户同时制作；连续提交不挡别人；同集一次传输；同基底一次生成、不同 Hook 独立成片；暂停/删除不损坏共享读取；同盘预留不超卖；重启不重复上传；默认串行配置与旧任务仍可用。

---

## 12. 对现有设计稿的取舍

现有 `docs/archive/multi-user-concurrency-design.md` 方向正确，但作为架构提案，我建议三处收敛：

1. **把"资源类型分池 + 等待不占位"提到最前面**，它是收益最大、依赖最少的第一步；现有稿件把"协议完备性"标成 P0，容易让人误以为不做全套不能上线。
2. **弱化 SIGSTOP 方案**：协作式安全点暂停是唯一跨租户稳健的做法；共享写者一律不响应单用户暂停。
3. **把"外部写恰好一次/崩溃窗口恢复"拆成独立可靠性线**，不要堵住阶段 A/B；它和并发正交。
4. 增加**容量校准流程**：并发数由 `render` 双任务实测（内存峰值、线程、`pids_limit`）标定；生产容器额度为 14 CPU / 24 GiB（见 [服务器规格](server-sizing.md)）。
