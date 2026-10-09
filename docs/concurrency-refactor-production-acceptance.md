# 并发改造 · 生产真实验收记录（2026-10-09/10）

环境：美东生产实例 `8.221.122.75`，宿主 16 vCPU / 约 30 GiB，容器 `cpus=14 / mem=24g / pids=512`。
部署版本 `us-east-20261010-concurrency`，commit `399917136fe1c665a20b4357111608ea30ddedb1`，镜像 `sha256:7f9727bfc4b7684187284fff53b571040dcddebee7f89c270d10959e1273091a`。
回滚目标：`us-east-20261009-1`（image `sha256:3986618e599deb32d6edf6c65ba5a2b10d553e9686444bde15915807d6d889f7`）。

## 1. 代码与测试（三方一致）

| 项 | 结果 |
| --- | --- |
| 代码提交 | `e142997`…`3999171`（第 1–3 步 + 三轮审查修复），分支 `concurrency-refactor` |
| 本机 macOS 全量 pytest | **677 passed**，连跑 3 次均 exit 0（另干净 clone 677 passed） |
| 发布构建内 Linux 容器回归 | **670 passed / 7 skipped**（跳过项为 Docker/Node 环境检查） |
| 独立复审 | Grok 第 1–4 轮；第 4 轮结论「P1-A/P1-B/偶发测试均已修复，无新 P0/P1，可进入生产真实验收」；审查记录见 `docs/concurrency-refactor-grok-review.md` |
| TDD | 每个改动先写失败回归再实现；证据见 `docs/concurrency-refactor-implementation.md` |

## 2. 发布与生效配置

- `linux-ops build` → `check-switch` allowed=true → `publish` 成功：`us-east-20261009-1` → `us-east-20261010-concurrency`，healthz `status=ok / environment=production / worker_alive=true`。
- 容器生效 `Effective concurrency settings`：`fetch=2, render=3, upload=2, poll=4, max_active_children=8, hook_previews=1, ai={6,1}`。
- 容器限额实测：`NanoCpus=14000000000`、`Memory=25769803776`、`PidsLimit=512`、`io.exactcut.stage-protocol=1`。
- 宿主 `/srv/exactcut/config/runtime.json` 的 `concurrency` 已与生产模板一致（sha256 `1276eac15cc43f631839cd41be0a8ce7bc1a38b2c8203c91a0ac3f81f17b211e`）。

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

## 6. 遗留测试数据

生产数据根生成了 6 个验收批次（剧 101460）：`验收A`（EP1 00:05-00:20）、`验收B`（EP2 00:05-00:20）、`验收C`（EP1 00:30-00:45）、`验收D`（EP2 00:30-00:45）、`验收E`（EP1 00:00-01:00，崩溃恢复）、`验收F`（EP1 00:00-00:20，守卫测试）。均为 `review` 状态、未上传。可按需在界面删除。
