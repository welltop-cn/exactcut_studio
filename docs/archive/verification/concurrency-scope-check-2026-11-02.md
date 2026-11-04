# 并发修复范围审查与失败映射（2026-11-02）

> 历史归档（2026-11-04）。正文保留当时的方案或执行状态，不代表当前版本；日常操作见[文档入口](../../README.md)。

新增功能范围、任务类型和队列抽象保持冻结。隔离分支`feat/frontend-background-resource-scheduling`，基线9b4b2bb；基础源码候选`abd089fda3f2b60ed66e8d2359348d2f6860bd8b`，父review补丁`81d66ac`、`96c1aa5`，当前固定源码为96c1aa5。以下保留中途失败和原断言映射，不能将历史失败或中断当通过。18883在审查过程中保持未动；父最终review通过后已于2026-11-03按明确授权空闲维护更新，详情见frontend-background-user-upgrade-2026-11-03.md。

新固定源码96c1aa5已完整回归：**2057 passed、1 skipped、10既有fork警告，673.17s**，日志`/tmp/d1-resource-full-96c1aa5.log`。父独立14项验证及新源码worker0实际development入口也通过；旧abd089f模型混合证据仍明确保留原版本。

基础候选abd089f全量：**2049 passed、1 skipped、10条既有fork警告，660.13s**，日志`/tmp/d1-resource-scheduling-full-abd089f.log`。实际dev模型核心证据仍明确属于abd089f；不能以此冒充96c1aa5已重复模型实测。新补丁实际worker0 development核验通过，详细重验范围见交付文档；当前线上runtime仍未实读。

## 当前体量

新固定源码96c1aa5相对9b4b2bb：源码/测试83文件、2500增/727删。基础abd089f为81文件、2302增/727删，其中生产33文件1057/427、测试48文件1245/300。父review只增加实际main与Hook源身份两份测试文件及两个现有生产文件的最小修复；五个新增生产模块仍445行。增长只来自下文已复现的生命周期、outbox、报告锁和冻结正确性修复，没有继续增加任务类型。

首批中途快照原为tracked43文件、735/511，新生产模块408行，保留作范围演变依据。

## 新模块职责及必要性

|模块|唯一职责|为何属于当前并发目标|
|---|---|---|
|interactive_queue.py|保存pending/active与owner/组游标，复用原worker，领取所需执行租约，有限dispatcher线程与恢复|替代满额HTTP429和每请求立即开线程；不重写普通/final/字幕队列，不做统一DAG或工作流系统|
|editor_preparation.py|既有cuts/mosaics原片snapshot的异步HTTP适配|原`open`持全站prepare.lock并在HTTP复制/hash/探测，忙时直接拒绝；只删锁会无限复制。现复用相同队列preprocess2，缓存命中仍200，冷路径202可查询|
|editor_frames.py|冷准确帧进入短媒体2，热JPEG直接返回|仅换全局frame.lock为key锁会让大量拖动请求起等待线程或继续429。现独立短资源，不排在长制作后，同用户同会话等待旧帧被最新请求替代，不改变帧精度/窗口算法|
|resource_locks.py|按真实identity的跨进程锁与继承FD|替换OSS64散列碰撞和index无同key生成锁；不是调度平台。guard继承已被真实父死亡红灯要求|
|execution_profile.py|本次相关参数的有效快照和相等断言|仅25行，对照普通/字幕/final/资源/线程；没有配置平台、线上巡检或自动调参|

## HTTP协议改变（必须按新契约验收，不能假装兼容同步语义）

- scan/locate/track/clip原job字段保留，新增queued/superseded等状态；仍是原POST/GET路径，忙时已接收而非429。重复身份复用。
- 冷`/cuts|mosaics/open`从同步200改202；`/preparation/{id}`给状态，completed.result.session是原session。缓存命中仍200。前端两编辑器已补轮询；关闭仅停止等待，不取消已登记准备。
- 冷`/frame/{N}`改202 JSON，同URL轮询生成后200 JPEG；热命中仍200。EditorBusy.frame统一适配两编辑器，首次显式允许重试先前失败，后续轮询失败明确结束。旧直接取blob客户端不能不更新。
- Hook原POST从同步结果改job回执202/200；GET hook-previews列表及id恢复。result包原Hook结构，input_current用于拒绝旧输入结果。前端仅对应按钮/局部状态等待，后台忙不锁整个项目。preview_key回执不算输入变化，不清理助手模块。
- task/report仍schema1、旧报告可读；新增交互队列非空时不能直接回退旧客户端/服务，需排空或取消后回退。普通/final/字幕格式及队列权威不重写，不迁移用户DB。

## 既有113项专项的首轮16失败逐项分类

首轮`/tmp/d1-existing-editor-queued-targeted.log`为97 passed /16 failed，不能当已通过。对应修复后的113项通过（87.01s，`/tmp/d1-existing-editor-queued-updated.log`），之后也纳入固定候选完整回归。下面保留改变依据与原业务断言。

|失败测试|分类|处理及保留内容|
|---|---|---|
|mosaic_locate::provider_single_jpeg_refusal|已取消Gemini RPM后过期测试桩|移除scan.ai_rate桩，仍检查单次真实SDK方法调用、空响应失败、client关闭和诊断；不允许伪成功|
|mosaic_locate::sdk_normalization_does_not_change_shared_schema|同上|仅移除无效预算桩；保留Schema不被SDK修改|
|cutting_scan::detector_preserves_coarse_on_provider_refinement_failure|同上|仍保留供应商复查失败时粗筛建议和诊断，rate_wait改为不存在、model_request仍存在|
|mosaic_locate::snapshot_open_and_frame_are_independent|正当冷HTTP协议变化|明确断言初次200/202，再按公开receipt查询；仍检查独立文件/字节、JPEG、越界及跨项目404，不把202假改为200|
|mosaic_locate::running_job_does_not_hold_edit_gate_and_rejects_stale_result|旧每账号单槽拒绝预期失效|第二seed应接收queued；原运行者源码lease、编辑gate、reset失效和结果拒绝仍须保留，第二任务也须收敛退出|
|mosaic_scan::running_reset_and_global_lease|同上|scan时locate应接收，不能保留容量CutError；原batch保护、reset变化导致结果拒绝仍保留|
|mosaic_scan::scan_logs_lifecycle_and_request_context|**实际回归**|新持久队列丢了原trace/client_request_id；已只持久化必要诊断ID并在worker恢复context，原断言不放松|
|cutting_scan::scan_http_lifecycle_cache_recovery_and_acl|旧账号容量429预期失效|strict与standard各身份可queued；认证/CSRF/非法policy、同job去重、source lease、中断恢复、错误脱敏断言仍保留|
|cutting_api::access_and_source_identity|正当冷open/frame协议变化|保留首响应真实状态，follow preparation/frame；认证/CSRF/SHA409、跨项目404/越界400/JPEG/原片SHA全部保留|
|cutting_api::render_save_reopen_and_source_immutable|同上|只补准备等待；实际FFmpeg结果帧数79、重复任务、save幂等、输出/保存版本/原SHA不放松|
|cutting_api::mosaic_placeholder_rejected_by_api|同上|先等session，之后旧不支持mosaics仍422、jobs空，不删能力边界|
|cutting_api::discard_fences_stale_window_mutations|同上|先等session，reset和旧窗口409、非法reset、合法新reset仍检查|
|cutting_api::source_stream_works_while_compute_and_frame_locks_busy[cuts]|同上|只等准备；持锁时原片流与字节完整仍验证|
|cutting_api::source_stream_works_while_compute_and_frame_locks_busy[mosaics]|同上|同上，不修改原流保护|
|phase2_preview_api::long_preview_does_not_block_reads_or_saves_and_rejects_stale_result|正当Hook异步协议变化|202只说明接收；保存需<1s，后续report必须superseded且不能发布旧缓存，不把旧409直接删掉|
|phase2_preview_api::preview_saves_complete_cache_only_after_successful_recheck|同上|follow job完成后检查原完整缓存及内容，不能只断言202通过|

## 父审查原明确缺陷（均已按下文红绿记录收敛）

1. `Dispatcher.stop(timeout)`可能在worker仍活时释放dispatcher lease：真实有限timeout停止测试必须证明第二owner不能接管，待旧worker退出后才能释放。修复要跟随既有字幕dispatcher的所有权原则。
2. claim/loop缺维护检查和领取事务屏障：维护时pending保留；撤维护后执行；与stop竞争时不新增工作。必须红绿复现。
3. 父提醒的supersede/claim报告写锁与outbox缺report路径：先在以上两项后复现，不能靠删断言。确认有失效后纳入本队列正确性修复；不借机扩大任务框架。

## 未变的业务额度与证据边界

候选有效配置普通fetch2/render3/upload2/poll4（无额外总1）、最终mosaic1、字幕2+30。独立本地dev须用同候选配置验证；18883的旧各1/总1配置没有被拿来冒充生产。媒体共享5+final专用1已取得下述Mac代表场景功能证据，仍不是Linux/长掩码最大承载或生产核定容量。当前线上runtime没有实读，不声称已与线上参数核实一致。

早期29项专项不作为交付依据；之后固定源码全量2049通过。当前真实dev已记录4真实Gemini与7次六帧CPU SAM、3条普通FFmpeg与final/两路SAM交叠、实际独立Chromiumqueued/关闭恢复/播放，以及实际最终视频和普通QC结果。媒体5+1仍只获Mac代表场景功能证据，不升级为Linux容量或生产核定；没有读取或覆盖线上runtime。

## 已固定的源码提交及证据提交

1. `9860f90`：Gemini/文本Excel预算、解析短发布事务（兼容离线调用方事务）、仅隐藏助手入口。
2. `fa42015`：媒体真实identity锁、短/长/transfer租约、SAM继承FD及实际子进程所有权。
3. `abd089f`：持久交互接收/原worker/HTTP与前端queued恢复/报告正确性，同时提交同文件相关的普通去10、字幕30、候选配置与有效值对照。
4. 证据文档单独提交，不让文档提交冒充应用新版本。

业务配置与交互协议在相同启动/HTTP文件中相互依赖，因此合并在第3项，不能单独部署这些中间提交。以下原五个关注点仍可在diff和矩阵中独立审查：

1. AI限频/解析短事务/助手隐藏（可逐项复用，不删除共用模块）。
2. 普通接收去10、字幕30、同版本候选并发基线及小范围有效值检查。
3. 交互持久接收、原worker适配、前端queued/恢复/局部busy行为及必要trace恢复。
4. 媒体真实key锁、短/长执行资源、SAM继承FD与取消/父死亡保证；明确对第3项依赖。
5. 隔离dev逐项入口证据、配置对照、最后全量与剩余限制。

以上是关注点拆分，不会让未完成的交互队列单独部署。父review通过前18883不更新；范围外问题只列待用户审批。

## 后续278通过/20失败的逐项原断言映射

原始日志`/tmp/d1-budget-business-resource-targeted.log`。这批结果不是交付结果；之后修改仍需稳定候选全量。下面逐项保存失败依据，不只删除Semaphore/额度断言。

|原测试/参数|类别|原断言 → 替代业务断言|
|---|---|---|
|round1_scheduler::startup_logs_merged_effective_limits_before_initializing_data[None]|配置契约变化|无runtime段时总1 → 集中default候选2/3/2/4、总11、启动日志等于实际合并配置；仍在初始化前记录|
|同上[segment1/render3]|配置契约变化|部分段余lane各1/总6 → 继承同版本default2/3/2/4总11；显式total8案例保持8|
|source_frames::index_rejects_non_draft_and_bounds_probe_work|执行契约变化|占process-local _SLOTS后拒绝 → 占集中media执行池时真实索引worker等待、没有index发布，释放后实际FFprobe完成；非draft仍拒绝|
|editor_resources::two_users_preview_while_final_held_but_actor_and_global_limits_apply|移除账号提交额度|账号第三次当前账号错误/4FD → 同账号或异账号都可用两个执行slot，第三执行者ResourceBusy，租约真实2FD；最终compute锁不阻断交互|
|editor_resources::roles_are_independent_and_configurable_without_changing_model_config|测试依赖旧错误文案|超3执行slot必须busy仍保留，改typed ResourceBusy而非文案包含3；scan/preprocess独立、model配置不变仍保留|
|editor_resources::account_and_global_quotas_are_shared_between_processes|移除账号额度|跨进程账号拒绝 → 子进程占一个后同账号仍可取第二全局slot、第三执行者全局busy、子退出后可重取；实际子进程及FD共享保留|
|editor_resource_busy::scan_admission_failure_has_no_job_and_releases_earlier_locks[scan-cuts]|接收契约变化|无job/ResourceBusy → queued报告+ledger存在，mock Thread.start确保HTTP不建等待线程，worker锁与未用执行角色不泄漏|
|同上[scan-mosaics]|同上|同上，保留mosaic源身份与状态路径|
|同上[compute-cuts]|同上|preprocess满仍接收且scan执行slot可用，不提前占有等待链|
|同上[compute-mosaics]|同上|同上|
|editor_resource_busy::non_capacity_scan_admission_never_retryable[worker-cuts]|执行所有权延后|worker锁忙直接拒绝/无job → 真实HTTP202 queued，不消耗scan执行slot，仍等实际worker租约；非capacity通用锁单测保持typed/generic差别|
|同上[worker-mosaics]|同上|同上|
|同上[batch-cuts]|原测试边界迁移|模块start内部错误 → 真实HTTP仍泛429、无code/retryable且无报告；不放松batch变更屏障|
|同上[batch-mosaics]|同上|同上|
|diagnostics::media_timeout_retains_cause_without_command_secrets|测试桩过期|patch subprocess.run → patch新的实际processing_cancel.run；保留timeout事件/脱敏，并新增__cause__仍TimeoutExpired|
|diagnostics::preview_admission_logs_exact_budget_and_release|移除旧Hook提交额度|inline quota1拒第二token → dispatched worker磁盘可独立预留，原hook_previews键不形成提交门槛；两个token均释放、低磁盘仍ResourceDeferred+原reason/context|
|diagnostics::preview_http_cleanup_and_compute_deferral_are_observable[success-200]|Hook协议变化|同步200 → 202后真实report completed且结果key一致、active退出及INLINE/磁盘ledger释放|
|同上[error-400]|Hook协议变化|同步400 → 已接收202，后续failed；cleanup仍检查|
|同上[deferred-429]|协议变化+实际诊断回归|同步429 → queued且持久not_before，不重新提交；新worker初版遗漏旧hook_preview.deferred事件已恢复phase=compute，原诊断断言保留|
|同上[cleanup_error-200]|Hook协议变化|同步200 → completed，不因cleanup错误伪失败；仍必须有cleanup_failed事件|

过程中新增测试曾误用索引_public不存在的frame_count，是**测试错误**，已按真实契约key/episode/fps/times_ms/duration_ms改times_ms与duration_ms，不增造API字段。诊断断言也改为等待对应active退出后检查释放，避免在completed发布与finally清理之间读两个不同快照造成假失败。

## 同轮新队列及UI回归复现记录

- 有限stop、maintenance、stop/claim竞争、global mutation屏障：红4项，修复后原queue+新生命周期9项通过；coordinator最后join所有worker后放owner lease。
- 缺报告outbox：红2项（同身份重试GET无报告、新参数supersede直接FileNotFound）；仅从已提交descriptor.job恢复，未新增持久权威。
- queued取消绕state_lock：红1项，修复后12项queue组通过。取消报告在queue锁外非阻塞获取原state_lock后重读；ledger仍保留pending直到报告终态。
- receipt与workbench.v3共用前缀：现有真实浏览器草稿枚举错误已复现；新增receipt独立preview-job.v1，旧draft键与schema不改。
- 晚ack覆盖新resume指针：单页红1项；跨tab红1项。由每素材独立receipt.request随机token验收，旧回执不覆盖新指针，不触碰草稿。真实Chromium5项覆盖关闭重开、显式stop失败迟到、最终queued冻结、晚ack、双tab/刷新恢复/切素材；`/tmp/d1-preview-receipts-green.log`为5通过/4.76s。API在这些UI夹具中明确mock，不能称真实模型验收。
- 缺concurrency整个段的旧总1分支仍残留：lane互相阻塞的两项新策略测试红灯后删除该分支，保留显式应急total行为；生产有效对照明确记录为候选基线，不声称线上实读。
