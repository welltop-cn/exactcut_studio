# ExactCut main 发布差异审查（2026-11-08）

## 结论

基准为服务器发布管理器及健康接口实读的生产 `79b2ca863b36b7b2b4661546d25de678417388c4`，原 main 为 `e31abe76f33c5acaeddd8847c971c684f402d7c3`。审查发现并复现 1 个发布阻塞：纳管与旧项目并发 sources 请求存在锁顺序反转。已用独立提交 `de8d7e73fee243cefd08577536ac706112454aac` 修复并推送 main。

| 严重程度 | 数量 | 状态 |
| --- | --- | --- |
| 高（服务请求死锁） | 1 | 已修复，有红绿回归 |
| 其他已确认阻塞 | 0 | 受下述覆盖边界限制 |

最终建议：**通过并发布修复后的 de8d7e7**。本机全量、Linux 发布门禁及 Linux 浏览器补验均通过，详见[发布记录](production-main-2026-11-08.md)。代码审查未发现鉴权检查移除、外部交付重放路径新增、依赖或持久 schema 变化。

## 变更范围

原范围 `79b2ca8..e31abe7` 共 21 个提交（包含合并及发布记录），44 个文件，4832 行新增、318 行删除。应用文件 15 个，其余为测试、辅助夹具和文档。

| 改动 | 文件及行为 | 风险与审查重点 |
| --- | --- | --- |
| 锁与阶段诊断 | `lock_timing.py`、`batch_control.py`、`webserver.py` | 重入语义、锁外日志、异常不改变业务、路由标签不带账号与查询参数 |
| 单轮读取复用 | `tick_snapshot.py`、`runtime.py`、`util.py`、`webserver.py` | worker 线程 scope、stat 校验、写入失效、任务副本、恢复后继的可见性 |
| 已结束项目跳过 | `finished_skip.py`、`material_workers.py`、`webserver.py` | 无动作才记忆、输入变更失效、定时全扫、时间/进程相关项不跳过、交付意图保护 |
| 前端切换与诊断 | `web/app.js`、`web/diagnostics.js`、`webserver.py` | 切项目推迟轮询，150ms 原片查询及预取，取消过期结果，批量日志保持鉴权、大小与频率限制 |
| 预取去重 | `source_prefetch.py`、`webserver.py` | 保留 queued 状态，同项目/实例/剧集去重，失败需显式 retry，锁顺序统一 |
| 坏任务隔离 | `job_records.py`、`storage.py`、`workflow.py`、`cutting_access.py`、`webserver.py` | 只读与隔离分开，终止执行器先核实身份，确认退出后释放预留，损坏交付保持 unverified |

## 已修复的高风险发现

位置：原 `source/exactcut/webserver.py:258`（`e31abe7`）；修复 `de8d7e7`。

纳管 `Handler.material_submission` 的 sources 路径不预持 web.LOCK，旧 `handle_post` 路径预持该锁。原 `prefetch_sources` 先持 PREFETCH_LOCK，`stored_jobs -> read_job` 再取得 web.LOCK。已有任务时，两个合法请求可形成 LOCK/PREFETCH_LOCK 反向等待，阻塞请求和需要全局锁的 worker。调用方仅需能对其有权访问的项目提交正常 sources 请求，不需要构造损坏 JSON。

修复为 `with LOCK, PREFETCH_LOCK`；web.LOCK 已为 RLock，旧入口合法重入。去重判断、保存、入队仍在同一临界区。

回归位于 `test_sources_prefetch_dedupe.py::test_mixed_managed_and_legacy_requests_do_not_deadlock`。先创建已有任务，再让两个线程分别模拟旧入口预持锁与纳管入口调用，对预取锁使用有界等待以避免测试永久挂住。

- 空任务目录的首次探针通过：未执行 read_job，不能当作有效复现。
- 有已有任务的原实现：`1 failed in 1.97s`，异常“素材预取锁与全局锁互相等待”。
- 修复后相关集合：`91 passed in 10.52s`。验证两个调用结束且仅保留一份预取任务及队列项。
- 原候选的本机/Linux 全量已主动取消，不能作为通过证据；最终回归固定在 de8d7e7。

## 调用影响及历史依据

按 Python AST 统计 `source/exactcut` 中静态调用位置；这是调用点数量，不是运行时覆盖率。

| 接口 | 调用点 | 重点 |
| --- | --- | --- |
| `stored_jobs` | 21 | 页面、管理器核实、资源统计与 worker；只读默认不隔离 |
| `read_job` | 4 | 原始 JSON 可缓存，校验及隔离每次执行 |
| `prefetch_sources` | 2 | 纳管入口和旧入口，均须统一取锁顺序 |
| `recover_jobs` | 2 | 启动与 tick 恢复 |
| `rebuild_reservations` | 1 | 隔离先于预留账本锁，避免递归文件锁 |
| `recover_outbox` | 1 | worker 锁外恢复，保留原候选/交付业务条件 |
| `signal_identity` | 5 | 所有调用继续经实例、token、PID/PGID 核实 |

基线 blame 显示 `stored_jobs` 的容错扫描源于初始代码，诊断由 `42ff3ae2` 增加；outbox 来源于 `5e8cefe7`，一次任务快照由 `b90fb4cb` 引入。此次改动强化结构校验与持久隔离，未删除这些路径的授权或远端意图核实。预取独立锁来自 `c1c1710`，后续引入 read_job 全局取锁后形成组合回归；仅审查各自分支的空队列用例不足以发现它。

## 测试覆盖与边界

已有测试覆盖调度三轮行为、并发限额、暂停/归档、恢复接管、同轮补位、缓存外部写入/同大小替换、finished-skip 多轮等价性、跨账号接口快照、损坏任务结构及解析、身份不匹配不发信号、执行器终止未确认不释放预留、交付意图不重放、HTTP 只读不隔离、过期前端请求不覆盖当前项目。

最终固定提交 de8d7e7 的结果如下；各批次不合并计数冒充独立用例总数。

| 检查 | 结果 |
| --- | --- |
| 本机完整 unit + integration + UI | 2811 passed、10 skipped、10 warnings，797.17 秒，退出码 0 |
| Linux 发布管理器完整回归 | 2397 passed、424 skipped、10 warnings，765.09 秒，退出码 0 |
| Linux Chromium UI 补验 | 401 passed、8 skipped、2412 deselected，173.57 秒，退出码 0 |
| Linux 未标 ui 的浏览器媒体集成补验 | 7 passed，19.51 秒，退出码 0 |

本机与 Linux 的 10 条 warning 均为已有多线程进程使用 fork 的 DeprecationWarning。Linux 标准验证镜像无 Playwright/Chromium，另外在既有 Linux 浏览器验证镜像中只读挂载同一候选，禁用网络与业务外写运行 ui 层，以及 `test_mosaic_queue_browser_http.py`、`test_validation_http_ui.py` 两个集成文件。浏览器补验的 8 项跳过没有计作通过；标准门禁的 424 项跳过也没有整体改记通过。本机全量仍保留 10 项已有跳过。浏览器验证镜像的 Python 为 3.12.15、pytest 9.1.1、Chromium 143.0.7499.4；应用 requirements 相对生产未变。

人工审查覆盖全部 15 个变更应用文件；对测试按用例与风险检查，没有计算逐行覆盖率。审查结合 diff、基线 blame、直接调用方和一跳依赖，不是整个仓库安全审计。没有触发真实 AI、下载、成片生成、OSS/FBBM/飞书写入，也不宣称生产压力与端到端交付已验收。损坏 JSON 无法恢复丢失的执行器身份；隔离告警仍为现有 best-effort 机制。

私有红绿证据：`/Users/matao/exactcut-test/release-main-e31abe7/`。最终回归及上线证据：本机 `/Users/matao/exactcut-test/release-main-de8d7e7/`、服务器 `/srv/exactcut/preparation/main-de8d7e7/`。
