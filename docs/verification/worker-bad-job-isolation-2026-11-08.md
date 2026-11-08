# 坏任务逐条隔离：实现与验证

本轮基线为 main `1c121f4`；实现已完成审查，按本报告范围本地提交。未部署、推送或发送通知。没有修改 `prefetch_sources` 或 sources 预取去重逻辑。

## 实现边界

- `job_records` 提供共享读取及最小结构验证。`stored_jobs` 只读过滤，在排序、集合构建和批次路径拼接前验证 ID、时间、attempt、command、调度字符串字段与 admission；保留无 batch 的历史任务。合法 ID 但缺草稿的 running 预取也隔离。
- 可解析坏任务在原任务中持久化 `quarantine.reason` 和终态；保留损坏字段作为调查证据，后续调度不再使用它。pending 重新读取持久化记录；owned 退出收割也先校验，移除队列项时配对 `task_done`。恢复尾部跳过没有草稿的孤立 runtime。
- 信号沿用数据根 instance、进程 token、PID/PGID 和进程表身份校验；也查找保存 PID 前的启动窗口。只向身份匹配的自有进程组发送 SIGKILL；确认根执行器消失或成为僵尸后才终结、释放预留。身份查询错误、信号权限错误以及短等待内终止未确认均继续向外报错，保留 running 与预留，禁止把仍活着的执行器隐藏成已失败。
- 发布坏任务保守进入 `unverified`。批次和 request_id 可安全定位时，将已有 publication 标记为待核实，保留 receipt 和 intent；不创建重传或交付重放任务。
- 截断 JSON、非对象 JSON、编码损坏原文件逐字节保留。旁车放在 `.web-state/job-quarantine/`，不进入 jobs 顶层扫描。旁车以设备、inode、大小、mtime_ns、ctime_ns 标识文件版本；未变化时不再读坏内容、hash 或写旁车，人工修改后重新验证。预留重建共享此读取边界，每条坏 JSON 不再中断重建。
- 告警为 `diag.event('job.quarantined', level='error')`，包含 job_id、batch_id、reason 和字典 error。未知 batch_id 为 null。测试保留真实 JSONL 和 prod_monitor 默认离线 scanner 的一次消费断言，不修改线上 feed 配置。
- 沿用原调度器的全局锁与 batch_control 锁顺序；全局锁改为可重入，允许恢复/HTTP 读取复用校验入口；HTTP 普通读取只过滤，不持久化隔离。没有另建改变业务先后顺序的调度通道。JSON 读取内部锁只防止同进程并发创建重复旁车。
- 不把存储权限、配置错误或进程表系统故障当坏任务吞掉。坏文件的旁车状态损坏也会暴露，而非静默忽略。

审查修复后相对 HEAD 共涉及 7 个文件（含未跟踪文件）：三个已有生产模块、一个新共享模块、两份测试与本文档。仅提交这 7 个明确文件。

## 测试意图和安全替身

第一阶段 admission 用例同时模拟了存活进程，又全局禁止 killpg。原断言若只要求 failed/释放预留，会允许真实活进程遗漏资源记账。本轮保留全局守卫，仅在这两个参数用例内安装专门的进程表/信号替身；替身限制目标组和 SIGKILL，并模拟进程退出。

另增明确测试：终止确认前不能释放预留；进程不退出时保留 running/预留并抛错；token、PGID 或 instance 不匹配时绝不发送信号。没有给生产身份校验放宽条件，也没有向真实非自有进程发送信号。

新增发布坏任务自身的 upload/poll × created/admission 损坏矩阵，检查原 intent 和回执、待核实状态及 Popen 次数；补 owned 退出队列计数、旁车稳定/人工修复、非对象/编码坏文件，以及权限错误不被隔离吞掉。

## 针对性结果

完整 stdout/stderr 保存在本次 run；不运行全量。

| 证据 | 结果 |
| --- | --- |
| 第一阶段原始回归 `regression-red.txt` | 22 failed，5 passed |
| 同一加强后的测试在基线源码运行 `strengthened-red.txt` | 33 failed，9 passed |
| 分支加强回归 `final-regression.txt` | 42 passed |
| worker/调度/恢复/预留配置及真实本地 lane 子进程集合 `final-related.txt` | 128 passed，36 deselected（包含加强前的 37 项坏任务用例） |
| 发布恢复、待核实回执及删除文件 `impl-delivery.txt` | 7 passed |
| 最后 ID 对齐收紧后的坏任务/预留复核 `final-check.txt` | 43 passed，40 deselected |

命令约定：`PYTHON` 为用户指定 venv，`RUN` 为本次 run 目录；从 source 执行。新回归命令：

```sh
EXACTCUT_DATA_ROOT="$RUN/test-data" "$PYTHON" -m pytest tests/test_worker_bad_job_isolation.py --basetemp="$RUN/pytest-tmp/final-regression" -ra
```

加强前中间集合为 115 passed、30 deselected；最初实现为 4 failed、23 passed，分别定位到无草稿 runtime 收尾和安全信号替身。错误工作目录导致的一次“无测试”输出也保留，没有计作通过证据。

HTTP 测试首次 socket PermissionError 属已知环境限制；本轮跳过 HTTP listener fixture，没有提权、关闭 sandbox 或绕过路径。预取 HTTP API、HTTP 幂等提交及 HTTP 管理接口没有本轮运行覆盖。integration 默认选择也造成部分 deselected；不能把 deselected 当作通过。

## 进程级端到端（非 HTTP 实例验证）

harness 只在 run 目录，未加入仓库。main 源码通过基线 archive 写入 run，不修改其他 worktree。main 与 branch 使用各自新建空数据根、独立 worker 子进程和进程组，真实执行启动 `recover_jobs(enqueue=True, scheduler=True)` 与 `worker()` 循环，不 mock Popen，不监听 HTTP，不调用 live 脚本。

worker 完成启动恢复后才注入排序更前的非法 batch_id、真实批次位置的伪造 draft queued 坏任务，及三个只写本地 marker、无 batch_id 的 legacy 全局任务。这部分仅证明全局任务启动，不能作为正常项目实证。

| 有效对照（e2e-v2） | 结果 |
| --- | --- |
| main | 连续 3 次 tick 失败；0/3 marker；后续任务未启动 |
| branch | 坏任务终结隔离；3/3 marker；三个真实子命令 exit_code 均为 0 |
| branch 运行中再注入截断 JSON | 原字节保留；总隔离告警 2 条 |
| branch 重启一次 | 启动恢复成功；tick failure 为 0；告警仍为 2 条；原坏 JSON 不变，marker 仍为 3 |
| 清理 | worker PID 5、13、24 均以 0 退出并被 harness wait；正常子任务 PID 21、22、23 均以 0 结束且确认已消失 |

这些 PID 是该次 harness 的进程命名空间内证据，不能用作后续服务操作目标。完整结果在 `e2e-v2/e2e-result.json`，各 worker stdout/stderr 和事件日志均保留。仅通过 Popen 返回的对象及其自建进程组停止自己的 worker；未操作任何既有服务。

首次 harness 将伪造 draft 放在错误的批次目录，因此 main 正常清理任务、marker 落地，未触发目标故障；该次实验无效，日志保留为 `e2e-harness.log`，不能作为修复前证据。有效实验重新使用全新的 e2e-v2 数据根。

## 剩余限制

- 这是进程级恢复/调度验证，不包含 HTTP 服务启动、鉴权或提交链路；外部交付只验证本地保护行为，未连接真实服务。
- 无法解析的 JSON 无法提供可信执行器身份，不能据此向任何进程发送信号；正常批次 control/owned 记账仍沿用现有机制。该边界不提供损坏文件中丢失身份信息的重建能力。
- 终止未确认或系统故障仍会使整轮显式失败；这是保留真实资源占用的安全选择，不能宣称所有系统故障也可同轮推进。
- 诊断日志沿用现有 best-effort 契约。隔离标记先落盘，随后发事件，避免重启重复告警；在两步之间进程崩溃或诊断落盘失败时，可能缺一条告警，不提供事务性 exactly-once 日志保证。
- 旁车按文件状态识别版本，不计算全盘内容哈希；没有验证人为保留全部文件身份及时间信息的对抗性替换。


## 审查回归修复（本轮，2026-11-08）

先补测试、运行红测后再修改生产实现。有效红测 `review-red-valid.txt`：**13 failed、50 passed**。复现 owned 已退出且文件已删除导致反复 FileNotFoundError、只读扫描发信号/写旁车、direct rebuild 错 admission/status、process_token=[] 未校验，以及真实 managed-parent outbox 在 batch_id=[] 上分组崩溃。batch_id=[]、status=[] 的 worker/recover 用例原实现已能处理，也保留为回归。

修复采用原隔离 helper 下移复用，没有增加扫描框架或修改素材业务：

- `job_records.read` 默认只读；`load(..., isolate=True)` 才进入调度隔离。普通 `stored_jobs()` 只跳过坏记录，不写 job、publication、预留、旁车或隔离告警，也不发送信号。worker 的 owned/pending 读取、recover、direct rebuild 和 outbox 明确使用调度入口。
- 只将任务自身 stat/open 的 FileNotFoundError 视为正常删除；权限及其他 I/O 错误继续暴露。测试覆盖 glob 后消失、stat 后 open 前消失及权限失败。owned 确认 poll 已退出后，即使持久化任务不存在也关闭 log、释放预留、移除 owned 并配对 task_done；同轮正常任务继续，后续轮次不重建被删除文件。
- direct rebuild 先共享校验与隔离，随后才获取预留账本锁，避免 helper 的 release 递归获取文件锁。真实身份匹配的活执行器先终止并确认退出，再写隔离终态/释放；终止不确认时仍抛错并保留原任务和预留。无法解析但调用方声明 live 的记录没有可信身份可供确认，保留其已有预留，避免无依据释放。
- 增加 process_token 字符串类型校验；batch_id/status/token 为列表时不进入后续路径、哈希和成员运算。outbox 原有一次任务快照在分组前复用共享校验，managed 父项目最小回归通过；不改变 candidate/delivery 的业务规则。原只读次数测试继续验证每文件一次读取；原来允许吞全局 OSError 的用例改为断言故障向外暴露。

新增测试首次通过后的检查中修正了两处测试预期：只读测试改为比较已有 fixture 日志读取前后的字节，而非断言没有建 fixture 时的日志；outbox unreadable 测试不再接受全局 I/O 错误被吞。`review-green1.txt` 保留这次 **3 failed、60 passed** 的完整输出。两次错误工作目录导致的无测试输出 `review-red.txt`、`review-related1.txt` 也保留，均不算验证通过。

### 重复验证与日志

所有证据路径均相对 `RUN`（本次独立 run 目录）。命令运行于仓库 `source`；使用 `PYTHON`（用户指定 venv 解释器），独立 EXACTCUT_DATA_ROOT 和每轮独立 basetemp。stdout/stderr 完整重定向到对应日志，pytest 临时数据与事件 JSONL 均保留。

| 集合 | 第一次 | 重复结果 |
| --- | --- | --- |
| 最终坏任务/调度/恢复/预留相关集合 | `review-related2.txt`：153 passed、36 deselected | `review-related3.txt`：153 passed、36 deselected |
| 显式 `-m ''` 的 managed verification / concurrency recovery / phase3 storage（排除 HTTP storage_api） | `review-integration1.txt`：52 passed、2 deselected | `review-integration2.txt`：52 passed、2 deselected |
| 含真实 owned 删除的进程级 E2E | `review-e2e-1/e2e-result.json`：success=true | `review-e2e-2/e2e-result.json`：success=true |

最终相关集合沿用前文命令中的文件列表，并额外包含本轮新增测试；`review-related1-valid.txt` 是新增末尾三项边界测试前的 **150 passed、36 deselected**。integration 显式启用此前默认 deselect 的 managed 测试，没有把 deselected 计作通过。未运行完整仓库全量，也未做发布。

进程 E2E harness 为 run 内 `review-e2e.py`、`review-e2e-repeat.py`，各自使用全新数据根，沿用基线 archive 对照和真实 worker/recover/Popen。额外包装 tick 只在执行完成后记录 owned/pending/WORK.unfinished_tasks，未替换调度逻辑或执行器。两轮均得到：

- main 连续三轮失败，0/3 正常 marker；修复分支原三个 legacy 全局子任务完成（无 batch_id，不代表正常项目）。
- 真实新增子进程登记 owned 后，先删除任务 JSON，再允许其退出；后续真实正常子任务完成。owned/pending 为空、unfinished_tasks=0、预留释放、删除文件始终未重建、该子进程确认消失。
- 截断 JSON 原字节保留；隔离告警合计两条；重启恢复后仍两条且 tick failure=0。
- 自建 worker 均以 0 退出并由 harness wait 回收；完整 PID、子任务退出码/消失证据和清理结果见两份 result JSON。各轮 worker stdout/stderr、事件 JSONL、tick-state 和 harness 日志全部保留。仅停止 Popen 返回的自建 worker 进程组。

没有监听 HTTP、打开 sandbox、绕过 socket 限制、接触既有服务或外部交付。按本次收尾要求本地 commit，不 push。前文 HTTP 与损坏 JSON 身份信息不可恢复等限制仍然适用。


## 收尾：正常项目级补证与提交检查

保留上述原进程级结果；其中没有 batch_id 的三个任务属于 legacy 全局任务，不能据此认定正常项目调度已验证。

本次另用 `RUN/project-e2e.py`，在 `RUN/project-e2e/` 下为 main 与 branch 分别创建全新空数据根。每侧三个正常任务分别绑定 `1`、`2`、`3` 各重复 32 次的合法十六进制 batch_id，每个批次都有独立 `draft.json` 与 `runtime.json`。action 为 prefetch；真实 Popen 子命令仅写各自数据根内 marker，不执行素材下载。worker 启动恢复完成后才注入坏任务及三个正常项目任务；未监听 socket、未外写、未操作既有服务，也未修改 sources/prefetch 段。

| 项目级补证 | 结果 |
| --- | --- |
| main 坏任务阻塞 | 连续 3 次 tick 失败，正常项目 marker 0/3 |
| branch 坏任务隔离 | 三个正常项目 3/3 completed、marker 3/3、exit_code 均为 0，tick failure=0 |
| 运行中截断 JSON | 原字节保留，隔离告警共 2 条 |
| branch 重启 | 恢复成功，tick failure=0，marker 仍 3/3，告警仍 2 条 |
| 退出与清理 | worker PID 5、13、30 均 exit_code=0 且 wait 回收；任务子进程 PID 21、22、23 均 exit_code=0 且确认消失；所有 worker 确认消失 |

PID 仅为该 harness 进程命名空间内证据。完整路径、命令、stdout/stderr、项目任务 JSON 与清理证据只留在 run 目录：`project-e2e.py`、`project-e2e-harness.log`、`project-e2e/e2e-result.json` 及对应数据根。此次证明有批次的正常项目能在坏任务后推进，不覆盖真实预取业务、HTTP 提交或外部服务。

收尾未重复运行 pytest、未跑全量。最终两份明确测试文件为 `source/tests/test_worker_bad_job_isolation.py` 与 `source/tests/test_scheduler_read_once.py`；已有独立复核输出 `RUN/owner-check.txt` 为 **66 passed**，用户也已独立运行同两文件。前文较大集合是收尾前已有结果，不是本轮新增运行。

删除 webserver 顶层未使用的 signal import，保留服务入口原有局部 import。报告用 RUN、PYTHON 或仓库相对路径表示位置；本机完整路径不入库。提交前检查 `git diff --check`、7 个明确文件的新增内容（含未跟踪文件）中的本机绝对路径、IP 与密钥，并确认 prefetch_sources 段相对 HEAD 未变。仅本地普通 commit，不 amend、rebase 或 push。前述终止未确认、损坏 JSON 无可信身份与非事务告警等风险仍适用。
