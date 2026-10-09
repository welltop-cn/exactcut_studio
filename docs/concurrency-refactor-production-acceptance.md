# 并发改造 · 生产真实验收记录（2026-10-09/10）

环境：美东生产实例 `8.221.122.75`，宿主 16 vCPU / 约 30 GiB，容器 `cpus=14 / mem=24g / pids=512`。
部署版本（当前）`us-east-20261010-ceiling-derived2`，commit `d7c048cd5854ff753d5ff6ed342a3da7fa96aae1`，镜像 `sha256:cf01ada129d9a5ed63d049ba42e76b22b5212eaa39aff98cf072d3209565fc85`。
上一版本 `us-east-20261010-concurrency`（commit `3999171`）。首次生产基线回滚目标 `us-east-20261009-1`（image `sha256:3986618e599deb32d6edf6c65ba5a2b10d553e9686444bde15915807d6d889f7`）。

## 1. 代码与测试（三方一致）

| 项 | 结果 |
| --- | --- |
| 代码提交 | `e142997`…`d7c048c`（第 1–3 步 + 三轮审查修复 + 全局总闸语义 + 测试确定性），分支 `concurrency-refactor` |
| 本机 macOS 全量 pytest | **677 passed**，连跑多次 exit 0；干净 clone 677 passed |
| 发布构建内 Linux 容器回归 | **669 passed / 7 skipped**（跳过项为 Docker/Node 环境检查） |
| 独立复审 | Grok 第 1–4 轮；第 4 轮结论「P1-A/P1-B/偶发测试均已修复，无新 P0/P1，可进入生产真实验收」；审查记录见 `docs/concurrency-refactor-grok-review.md` |
| TDD | 每个改动先写失败回归再实现；证据见 `docs/concurrency-refactor-implementation.md` |

## 2. 发布与生效配置

- `linux-ops build` → `check-switch` allowed=true → `publish` 成功：… → `us-east-20261010-ceiling-derived2`，healthz `status=ok / environment=production / worker_alive=true`。
- 容器生效 `Effective concurrency settings`：`fetch=2, render=3, upload=2, poll=4, max_active_children=11, hook_previews=1, ai={6,1}`。
- **全局总闸不再写进配置**：`max_active_children` 留空即自动 = 各 lane 之和（2+3+2+4=11）；改 lane 时自动跟随，不需手工维护；仅在应急降级时显式写一个更小的值。生产 `runtime.json` 与 `production.json` 模板均不含该字段。
- 容器限额实测：`NanoCpus=14000000000`、`Memory=25769803776`、`PidsLimit=512`、`io.exactcut.stage-protocol=1`。

## 3. 真实验收结果

真实素材：剧 `101460`，两名验收人（验收A…验收F），各自独立批次；**只做制作，不做外部上传**。

### 3.1 两用户并发（核心）
- T+20s：**两个 `fetch` 作业同时运行**（pid 81/82）→ 证明 fetch lane=2。
- T+40s：B 的 fetch 完成 → **B 的 `render` 启动**，同时 A 的 fetch 仍在下载 → **下载（等待/IO）不阻塞渲染（CPU）**。
- T+60s：**两个 `render` 同时运行**（pid 419/542），**两个 ffmpeg 同时在编码**（libx264 medium）。
- 命令实测：`-threads 1`（输入解码）、`-threads 4`（输出编码）、`-filter_threads 1` / `-filter_complex_threads 1`。
- 成片：A、B 各 15s、`qc_passed`，命名 `20261010_typezxraw_验收A_agent_1_#101460#.mp4`。

### 3.2 线程/RSS 采样（两路并发编码，不同时间线强制编码）
- 两路 ffmpeg 并存，每个 **NLWP=5–6**、**RSS≈510–560 MB**；与 `-threads 4` + 解码/滤镜/音频线程一致，**不是**每进程 12 线程。

### 3.3 崩溃恢复
- 在 batch E 渲染中（ffmpeg 活跃）`docker kill` 容器；`linux-ops recover` 拉起后健康 `ok`。
- 任务：`fetch attempt=1 completed`（不重做），`render attempt=2 completed`（从渲染阶段重排重跑）；最终 60s 成片 `qc_passed`；**无重复任务、无重复文件**。

### 3.4 回退守卫（只读检查）
- 队列排空时 `check-switch us-east-20261009-1` → `allowed=true`。
- 存在未完成阶段任务（`fetch/render` queued/running）时 → `allowed=false, exit=1`，`phase_blockers` 列出该 job；待其完成后恢复 `allowed=true`。

## 4. 未执行（需外部写授权）

以下场景**未执行**：它们会产生真实外部副作用（OSS 上传、飞书记录、Meta/FBBM 首次 ingest）。操作单已给出可直接执行的命令与证据字段，待明确指定**授权的验收账号与剧目**后执行：

- 完整链 upload→poll→`fbbm_ready`；
- 「等待 FBBM 不占执行位」的外部写实测（poll 查询之间无活执行器/预约，B 在此期间渲染推进）；
- 外部写结果不明的「待核实」放行、远端创建计数=0；
- 保守配置回退（把容量降回 1 / 关闭开关后重挂载）与旧代码回退的容器级演练（配置级与单元/集成级已验证，容器级未走）。

> 说明：「等待不占执行位」的机制已由 3.1 的「下载等待期间渲染并行」间接证明，并由单元/集成测试覆盖 poll 的 `not_before` 不占槽；但**FBBM 特定链路**仍需外部写授权后实测。

## 5. 回退

- 代码回退：`linux-ops rollback` → `us-east-20261009-1`；有未完成阶段/待核实交付时被守卫拒绝，需先排空或按操作单离线核实。
- 容量回退：改 `runtime.json` 的 `concurrency` 后重发布；`enabled=false` 或各容量=1 即回到串行，业务数据不还原。

## 6. 负载与容量实测（2026-10-10，生产）

### 6.1 render=3 并发编码

| 指标 | 实测 | 解读 |
| --- | --- | --- |
| 容器 CPU | **~950–1060% / 1400%** | 14 核约用 10 核，留约 4 核 |
| 容器内存 | **~1.6 GiB / 24 GiB** | 约 7%，内存极富余 |
| ffmpeg | 3 进程、共 **18 线程**、RSS 合计 **~1.6 GB**（每个约 530 MB） | 每路约 6 线程，与 `-threads 4` 一致 |
| 宿主 load average | **~6.6 / 16**（1 分钟） | 整机约 40% 忙 |
| 容器 PIDs | ~88 / 512 | 宽余 |

每路渲染约吃 **3.2–3.5 核**。14 核 ÷ 3.3 ≈ **最多 4 路**。`render=4` 可行但接近配额上限；`render≥5` 会超订。内存不是约束。

### 6.2 网络带宽（同一 OSS 链路）

- 单流下载：**~11–13 MiB/s**（app 报峰值 12.99 MiB/s）。
- 4 路并发下载不同集（合计 ~424 MB）：耗时 35.0 秒，**聚合 ~12.15 MiB/s**。
- 结论：链路上限约 **12 MiB/s（≈100 Mbps）**，**单流即接近饱和**；提高 `fetch` 只会分摊带宽、不增加总吞吐。保留 `fetch=2` 的价值是重叠与容错，不是翻倍。一集约 120 MB → 满速约 10 秒，下载非瓶颈。

### 6.3 瓶颈排序与建议

**CPU(render) > 网络(约 12 MiB/s 共享，够用) > 内存(极富余)**。提升吞吐应调整 `render`（3→4 上限）或增加容器 CPU，而不是增加 `fetch`。

## 7. 遗留测试数据

生产数据根生成了 6 个验收批次（剧 101460）：`验收A`（EP1 00:05-00:20）、`验收B`（EP2 00:05-00:20）、`验收C`（EP1 00:30-00:45）、`验收D`（EP2 00:30-00:45）、`验收E`（EP1 00:00-01:00，崩溃恢复）、`验收F`（EP1 00:00-00:20，守卫测试）。均为 `review` 状态、未上传。可按需在界面删除。
