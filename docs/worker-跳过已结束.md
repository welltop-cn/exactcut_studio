# worker 跳过已结束检查

缓存针对检查的输入和无动作结果，不按项目或任务的终态名称推断“永远结束”。接入点只有 `material_workers.recover_outbox` 的父项目循环和 `webserver.recover_jobs` 末尾崩溃恢复循环；终态任务补建后继、进程收割、资源恢复和调度顺序保持原样。

`finished_skip.py` 集中维护输入清单、易变条件和记忆。文件取 inode、size、mtime_ns、ctime_ns；缺失也是指纹的一部分。结果目录取自身指纹、成员列表和每个成员的指纹；媒体目录只取自身 stat，实际读取内容的媒体另列具体文件。新增、删除、原子替换会改变目录 mtime/ctime；具体文件的 stat 还覆盖原地内容改写。检查前输入与记忆相同才跳过；执行后再次计算，前后相同才记入，否则删除旧项。异常和损坏输入不记忆。job 快照的实际消费值也参与比较，防止快照读取后文件被替换，使旧快照的无动作结果误挂到新文件指纹下。

## 集中输入清单

| 检查 | 文件及目录 |
| --- | --- |
| outbox 父项目 | registry 标记、media-state.json、control.json、draft.json、access.json |
| 每个 outbox 子项目（按事件的 child_id 去重，不能按 outbox 键取子项目） | registry 标记、media-state.json、publications.json、draft.json、control.json、confirmed.json、runtime.json、access.json；production/results.json、production/results 目录及全部成员；production/videos、production/previews 目录自身；delivery 的指定交付视频，以及 verification 确认快照里的具体视频 |
| delivery | 通过 `media_file` 定位的父项目封存 master_path；materialize 会复制/校验它，并校验子项目交付视频 |
| verification | 子项目 production/edits 目录树，覆盖核实编辑成片的读取；全部 job 文件，覆盖 queue_verify_delivery 的 runtime/update 汇总 |
| 失效候选 | 仅存在通过取消前置门槛的 job 时列 reservations.json 和全部 job 文件；只有此分支才可能调用 storage.release 和 end_execution/update。关联 job、runtime、control 始终列入 |
| outbox 的环境归属检查 | 数据目录下 .web-state/environment.json，instance_marker/data_root 可能读取 |
| outbox 的 job 集合 | 属于上述 child_id 的 job 文件名集合、每个文件指纹和本轮消费值 |
| 崩溃恢复项目 | runtime.json、confirmed.json、publications.json、production/results.json、production/results 目录及全部成员、registry 标记、media-state.json、本项目 job 文件名集合、每个文件指纹和本轮消费值 |

jobs 不再依赖目录 stat。outbox 每轮先枚举 jobs 形成 snapshot；crash 在阶段恢复及后继补建后通过 stored_jobs 重新枚举形成 latest_jobs，再建立共享 JobSnapshot。两者都按 batch_id 索引，文件名随实际消费值一并比较，因此新增、删除和 batch_id 迁移会使对应项目失效；无关项目写 job 不影响普通项目。涉及取消汇总或 verification 全 job 扫描的父项目使用全部 job 文件名、指纹和消费值；reservations.json 仍只在可能实际取消 job 时纳入。

每个检查阶段末尾只重新枚举一次 jobs 目录，将文件名集合交给实际处理项的 finish 复用（直接调用 finish 时自行枚举）。当前文件名与阶段快照比较，读取新增文件的 batch_id。出现相关新 job（全 job 分支则为任意新 job）或读取失败时不记忆；这覆盖 materialize/_job/durable_write、advance_stage、requeue 等处理期间新增。已存在文件的改写和删除继续由前后指纹检查。跳过项不额外枚举目录，不缓存目录 stat。

逐函数核对结果：cancel_invalid_children/cancel_queued_candidate 涉及 control、draft、runtime、结果、job、reservations；reconcile_candidates 涉及父状态和 job；_merge_delivery 涉及父状态及子项目 publications；recover_media_upload/_job 涉及父状态、access 和环境归属；recover_verification 涉及 publications、确认快照、runtime/结果、成片和汇总 job；materialize 还读取父 access、子 confirmed、封存成片及交付视频。runtime → production_summary/result_records → result_store.read 没有读取当前时间，也不查询进程表；其中 managed 的判定既检查 registry，也检查 media-state 是否存在，所以两者均列入。

新增读取文件、增加目录枚举或改变 job 范围时，必须同步更新集中清单。新增当前时间或进程表依赖时，必须同步更新永不跳过条件。`tests/test_finished_skip.py` 用真实 C0 fixture 跑 delivery、candidate、pending verification、失效候选和 legacy 分支，拦截 builtins.open/io.open（包括 Path.open），要求实际读取是清单的子集，或是已列入指纹的目录的直属成员（目录 stat 覆盖成员新增、删除和原子替换）。目录不能替代内容依赖：delivery/verification 的实际视频必须另有具体文件清单断言，并测试原地改写和原子替换失效。唯一豁免由 `AUDIT_EXEMPT_PARTS`/`audit_exempt` 集中定义：锁文件和诊断日志；业务 JSON 不豁免。测试不能穷举未来新增分支，维护清单仍是修改恢复流程时的必要约束。

## 永不跳过

- 存在尚未 superseded 的 media-upload 或 verification 事件。保守地连 materialized/completed 标签也不据此跳过：核实恢复只把 superseded 当成终止门槛，且 job 丢失可能仍要补建。
- 关联 job 的 not_before 仍在未来；到期前每轮全量处理，到期后自然重新检查。
- 失效候选子项目 control.json 仍记录 children，取消逻辑依赖进程表。
- 失效候选的关联 job：batch_id 为该子项目、phase 为 fetch/render/media-upload，且 status 为 queued/waiting_resource，或为固定取消消息的 interrupted 且 runtime.status 属于 ACTIVE 或 executor_running；同时 belongs(job) 为真且带 PID 或 process_token。历史 completed、failed、其他错误的 interrupted 均不会因此被判易变。
- 输入损坏、读取/指纹失败、执行异常，或本轮前后输入变化，均不建立可跳过记忆。

崩溃恢复原有 live/queued 排除仍每轮执行，进程表查询保持在缓存外；缓存只绕过后面的 runtime 计算及无动作分支。

## 开关、兜底和线程

`EXACTCUT_SKIP_FINISHED` 默认开启，设为 `0` 时不建立或保留记忆，完全走全量；重新开启后先全量。缓存只在进程内，启动后首轮为空。默认每 300 秒在新 tick 开始时清空两种记忆；`FULL_SCAN_SECONDS` 可配置，`Memory(clock=..., interval=...)` 支持注入单调时钟和间隔。低频全量用于防守极端指纹碰撞和未来依赖遗漏。

调用点核对：outbox 由 material_versions 的薄封装交给 material_workers，产品代码仅 worker_tick 调用；recover_jobs 由 worker_tick 和 main 启动恢复调用，目前 HTTP handler 没有直接调用。实现仍防守其他入口：只有 worker_tick 显式进入线程本地 worker_scope；main 启动恢复、HTTP/CLI 未来的直接调用及其他线程一律全量。各 worker 线程拥有独立记忆，不共享可变缓存，无需额外扩大 web/batch_control 锁。跨进程或 HTTP 写入由输入指纹失效和前后双检识别；业务写入继续沿用原锁。

## 诊断和验证

现有 scheduler.tick_slow 字段保持原样，新增本轮 `outbox_processed`/`outbox_skipped`（处理/跳过父项目数）及 `crash_computed`/`crash_skipped`（计算/跳过 runtime 的项目数）。scheduler.tick_summary 中相同字段为汇总窗口累计数。仅统计实际到达对应循环检查的项目，原有 managed/live/queued 排除不计入。

回归包括原样保留的 C0 等价性断言、首轮/第二轮计数、开关、注入时钟的五分钟兜底、各类文件及 job 新增/删除失效、时间/进程条件、直接调用和跨线程全量、快照竞态，以及同一 fixture 重放两种模式十轮、四次注入后的逐轮 state_digest 完全一致。

上一轮验证：新测试与未修改的 C0 等价性测试每次 34 例，连续五次通过；指定相关文件显式包含 integration 后，347 例通过。另有 28 例既有 HTTP 测试受沙箱本地 socket 权限限制，1 例既有 flaky_fs 时间戳测试失败；后者已在当前 HEAD 原代码的独立副本复现相同断言失败，未修改其断言或产品行为。没有运行全量测试。

## 第二次收窄与成本核对

指纹读取父状态使用 B1 的 `tick_snapshot.parent_state`：同 tick 中依据 registry/media-state 的 stat 校验复用只读对象；原有写入失效与指纹前后双检继续保留。每个指纹只构造一次 child_id 集合，传给易变判断和输入清单；job 快照按 batch_id 一次索引，避免每个父项目反复扫描全部 job。跳过项只计算一次指纹，finish 也防守重复计算。已记忆的输入路径集合可复用，但每项 stat 和实际消费的 job 值仍每轮重新比较；结果和媒体清单包括对应枚举目录本身；job 则比较快照中的精确文件集合。相关成员变化、状态或 runtime/job 变化都会重建清单，不跨检查缓存 stat。

媒体依赖逐函数核对：`materialize` 读取封存 master 并哈希指定的交付视频，因此两个具体文件均保留；`recover_verification → approval_evidence → preview_file/media_path` 读取确认快照指定的视频或 edits 下的编辑成片，保留具体视频与 edits 树。`cancel_invalid_children/cancel_queued_candidate/reconcile_candidates/_merge_delivery/recover_media_upload` 不读取 videos/previews 的任意文件内容。`freeze/local_pair_valid` 虽会哈希 master/preview，但不在 recover_outbox 的调用链上，不能为它们逐文件扫描所有历史媒体。目录 stat 作为成员存在性的保守依赖；业务内容读取不能只靠目录 mtime。

反复处理原因已用三轮诊断实测：旧 token 规则把含历史任务的 28 个父项目误判易变，另有 2 个 pending verification。收窄 token 门槛后，这 28 个含失效候选的父项目仍然反复处理，因为其输入清单无条件包含 reservations.json。`storage.rebuild_reservations` 每轮在 reservation_lock 退出时原子写回账本，导致这些父项目在相邻两轮指纹间失效（两轮共 56 次），并非 recover_parent 执行前后指纹变化。只读盘点确认这 28 个账本依赖父项目与 28 个历史 token 父项目完全重合；这是收窄 volatile 后暴露的第二道失效原因，不能把两组数量相加。为核对原反馈的额外约 23 个，另在同一 pristine 副本恢复旧 volatile/输入枚举/指纹规则跑三轮，summary 的 processed 为 155、30、30，稳态 skipped 为 125、125（1619/1768ms）；未复现独立的额外 23 个，不能据此编造其原因。提供的快照上可证实的原因就是上述 28 个历史 token、其重合的账本失效，以及 2 个 pending verification。现在只有可能实际取消 job 时才纳入账本和全 job 汇总依赖；runtime 或关联 job 改变仍会重新进入检查，queued/waiting_resource/活跃 retired 的实际取消路径仍有完整依赖。新增回归覆盖账本重写后历史任务继续跳过、加入可取消 job 后重新检查。

## 上轮验证记录（目录依赖改造前）

新测试和未修改的 C0 等价性测试连续五次各 51 例通过。上一轮同一相关清单（显式包含 integration）本轮 364 例通过；28 例 HTTP 测试仍因沙箱禁止 socket 创建而失败/报错，1 例已知 flaky_fs 时间戳断言仍失败（上一轮已在 HEAD 独立副本复现）。没有运行全量测试。

线上规模按指定脚本独占复测 12 轮：逐轮 `total diff files: 0`，`api_before same: True`、`api_after same: True`。普通稳态及 i3/i7 注入轮为 994–1212ms，中位数 1073ms；同轮基线中位数 2202ms，下降约 51%。i9 写 job 触发保留的 jobs 目录全局失效，耗时 2860ms，因此“首轮后每一轮都 ≤1300ms”并未严格满足；其他十轮均满足目标。

下表耗时来自指定的完整 API/摘要对比。计数另在相同 pristine 副本跑相同 12 轮，仅将诊断 summary 周期设为每 tick、省略前后 API 调用；该副本每轮业务摘要也与完整对比一致。产品默认诊断周期保持不变。

| 轮（从 0 起） | 耗时 ms | outbox 处理/跳过 | crash 计算/跳过 |
| --- | ---: | ---: | ---: |
| 0 | 2840 | 155/0 | 748/0 |
| 1 | 1068 | 2/153 | 0/748 |
| 2 | 1104 | 2/153 | 0/748 |
| 3 | 1030 | 3/152 | 1/747 |
| 4 | 1022 | 3/152 | 0/748 |
| 5 | 1129 | 2/153 | 0/748 |
| 6 | 994 | 2/153 | 0/748 |
| 7 | 1035 | 3/152 | 0/748 |
| 8 | 1078 | 3/152 | 0/748 |
| 9 | 2860 | 155/0 | 747/0 |
| 10 | 1116 | 3/152 | 0/747 |
| 11 | 1212 | 3/152 | 0/747 |

稳定的 2 个父项目为 pending verification。i3/i7 各多处理被修改的 1 个父项目；检查前后输入有变化时不建立记忆，所以下一轮还会再处理一次，然后恢复跳过。i9 的全量失效轮后为 3 个（两个 verification 加一个带未来 not_before 的关联项目）；queued 项目沿用原逻辑排除在 crash 检查之外，所以后两轮 crash 跳过数为 747。

原始对比、逐轮 summary 与 profile 均只写临时副本；pristine 输入和基线 result 未修改。未 commit/push、未联网、未改前端。

最终独立 profile：普通无注入稳态 fingerprint 905 次、累计约 354ms（含 cProfile 开销），其中 finished_skip.stat 21216 次、累计约 64ms；整个 tick 的 posix.stat 为 60095 次，包含 B1 读缓存校验、调度器和其他恢复流程，不能把它与指纹内部 stat 混为一项。普通非 profile 预热后两轮为 1051/967ms。剩余 stat 不属于这次取消逐媒体枚举所能全部消除的成本；本轮未扩大到其他调度阶段。


## 按项目 job 集合依赖验证

本轮将 jobs 目录全局 stat 依赖替换为按 batch_id 的文件集合、逐文件指纹和消费值；全 job 分支仍取完整集合，账本门槛不变。新增 13 例回归，覆盖 outbox/crash 无关 job 新增、原子改写、删除时仍跳过；已跳过项目新增 job 后，通过实际 snapshot/latest_jobs 接入重新处理；处理期间新增相关 job 不入缓存、无关 job 不干扰记忆；全 job 分支对无关 job 集合及内容变动仍失效。索引对非字典记录不提前改变原恢复流程的异常行为，既有坏记录回归保持通过。

新测试与未修改的 C0 等价性测试连续五次各 64 例通过。相关清单显式包含 integration，最终 378 例通过；剩余 28 例均受沙箱禁止 socket 创建限制（4 failed、24 errors）。第一次相关测试中的文件时间戳用例在最终复跑通过；未改动该测试或对应产品代码。未运行全量测试。

最终独占复测十二轮，指定大数据脚本的逐轮业务摘要与关闭跳过的全量基线全部一致：`total diff files: 0`、`api_before same: True`、`api_after same: True`。首轮 3089ms；首轮后 1005–1129ms，中位数 1084ms；i9 为 1086ms（上轮为 2860ms），只处理 3 个父项目、跳过 152 个，crash 计算 0、跳过 747 个，未再整轮失效。

每个检查阶段末尾枚举一次 jobs 并复用文件名集合，避免首轮逐项目重复枚举的成本。下表耗时来自完整 API/摘要对比；计数来自相同 pristine 副本、同样十二轮注入的独立诊断运行（仅改临时脚本的 summary 周期并省略 API 调用），其逐轮业务摘要与完整对比逐轮一致。产品诊断周期保持不变。

| 轮（从 0 起） | 全量 ms | 跳过 ms | outbox 处理/跳过 | crash 计算/跳过 |
| --- | ---: | ---: | ---: | ---: |
| 0 | 2021 | 3089 | 155/0 | 748/0 |
| 1 | 2004 | 1106 | 2/153 | 0/748 |
| 2 | 1975 | 1053 | 2/153 | 0/748 |
| 3 | 2037 | 1032 | 3/152 | 1/747 |
| 4 | 2065 | 1083 | 3/152 | 0/748 |
| 5 | 2030 | 1005 | 2/153 | 0/748 |
| 6 | 2004 | 1084 | 2/153 | 0/748 |
| 7 | 1979 | 1125 | 3/152 | 0/748 |
| 8 | 2101 | 1075 | 3/152 | 0/748 |
| 9 | 2015 | 1086 | 3/152 | 0/747 |
| 10 | 2004 | 1127 | 3/152 | 0/747 |
| 11 | 1989 | 1129 | 3/152 | 0/747 |

稳态 2 个父项目仍为 pending verification；i3/i7 各增加受影响的 1 个父项目，检查期间输入变化则下一轮再处理后恢复跳过。i9 增加的 1 个父项目关联未来 not_before 的 queued job；该子项目按原规则排除在 crash 检查之外，因此 crash 总数降为 747。全 job 汇总分支仍保守依赖所有 job 文件集合，reservations.json 条件未放宽。

输入副本和基线结果保持隔离；未 commit/push、未联网、未改前端、未运行全量。
