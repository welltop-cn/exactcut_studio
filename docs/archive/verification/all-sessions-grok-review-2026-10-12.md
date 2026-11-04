# Grok 整体审查

> 历史归档（2026-11-04）。正文保留当时的方案或执行状态，不代表当前版本；日常操作见[文档入口](../../README.md)。

模型：grok-4.7-build。会话：01a12fdb-3905-73b1-b0e3-2e92ae396a59。续审正常结束 end_turn。

尾部已读完。还差质检时长、片尾规范化和预览名额这几处，我按已读位置核对，不重读全文。**通过。无阻塞问题。** 有 1 个已证实的非阻塞问题。

## 非阻塞

**进度条在合成片尾时没有当前步骤。** 本次引入，确定。

- 位置：`source/exactcut/web/app.js` 的 `productionProgress`。`stageNames` 已有 `ending_overlay: 合成静态片尾`，分组却是 `准备 / 剪辑拼接(base_cut,concat) / 字幕合成(frame_sampling,hook_overlay) / 质检(qc) / 生成预览(preview)`。
- 触发：成片阶段为 `ending_overlay`（`renderer.render_request` 在追加片尾时 `step('ending_overlay')`，`progress.STAGES` 也有这一阶段）。
- 影响：`groups.findIndex(...)` 得到 `-1`。步骤条上没有任何一步是 `current` 或 `reached`，五步都像未开始。徽章文字仍是「合成静态片尾」，字段和成片不受影响。
- 最小修复：在「字幕合成」与「质检」之间加入 `['静态片尾', ['ending_overlay']]`。
- 验证：做一条 `last_frame` 片尾，在阶段停在 `ending_overlay` 时看执行进度；「静态片尾」应为当前步，其前步骤为已到达。

## 结论依据

需求 1。`material_versions.detail` 只在没有候选版本、没有待确认代际，且当前草稿行的交付状态集合恰好是 `{fbbm_ready}` 时把项目标为 `complete`。空草稿、制作中、失败、`unverified`、`publish_failed`、在途交付都进不了这个分支。已删除行不在 `draft.rows` 里，不参与该集合。`queue_delivery` 对同一 `version_id` 且状态已是 `fbbm_ready` 的交付抛出「此版本已交付」。预览页在 `a.status==='fbbm_ready'` 时隐藏确认框、禁用上传按钮，并把按钮写成「已入库」。

需求 2。`admit_hook_preview` 只比较 `len(INLINE_RENDERS)` 和 `limits['hook_previews']`，不再看 `render` 或 `max_active_children`。磁盘预留仍在，预留失败会拒绝进入。`worker_tick` 已去掉把内联预览计入 `external_active['render']` 的那一行。`config/production.json` 的并发差异只有 `hook_previews` 从 1 改为 3，`render` 仍是 3。

需求 3。错误响应把 `trace_id` 写进 `error_info.detail` 的「诊断编号」。`/api/client-events` 先走 `browser_fields` 白名单，再以 `allow_browser_event(self.user['username'])` 做每分钟 240 次的账号上限；页面 ID、请求 ID 不在这个键里。超限返回 `200 {ok:true}`，不写事件。`event()` 自身吞掉写日志失败。

需求 4。`EndingOptions` 只接受 `none` / `last_frame` / `image`，时长 1000–15000 ms，引导文案最多 200 字。旧数据删掉 `mode` 后，`validate_draft` 得到 `mode == 'none'`。基础缓存身份是 `version: 3` 的时间线、源和 `base_render`，不含片尾；最终指纹包含完整请求，图片模式再加图片 SHA-256。片尾在 Hook 之后拼上，时长检查使用加上片尾后的 `expected_ms`。`snapshot_images` 把确认后的图片写成内容寻址文件名并回写 `options.image`。`schema_version` 仍是 1，`CutRequest.ending` 可选。

需求 5。编辑区按开头、剧情、结尾三节渲染。折叠后保存的载荷仍含字幕、时长和片尾字段。时间校对在新错误或下载失败时会重新展开。手工重点在 Hook 文案变化后清空并打开选词面板。

需求 6。`FeishuClient.get_record` 最多 3 次，仅对整数 `1255001`、HTTP 429、5xx、超时和连接错误退避。`workflow.poll` 在绑定完整且一致时写入 `verification_only`，之后只调 `verify_record_once`（读原飞书记录和 FBBM `sync_status`，无 ingest、resync、飞书写入）。`poll_started` 用 `setdefault`，自动重试不改它。绑定缺失、身份冲突、截止到期进入 `unverified`。成功路径执行 `entry.pop('error', None)`。暂停时运行状态改为 `paused`，因此不会被「自动重试」横幅覆盖。`_merge_delivery` 把子任务的 `error` 抄进交付的 `publication`，`guidance(..., 'fbbm')` 能认出这句前缀。

查询失败不会重放写入。`upload` 在外部调用前先落 `intent.status='pending'`；此后任何异常，包括 `publish_to_queue` 内部 `get_record` 的暂时失败，都变成 `unverified`，而不是再上传。`poll` 只有在 `verification_binding` 成功时才转只读；绑定不完整则 `unverified`。`delivery_blocked` 在 `unverified` 或 `intent` 仍为 `pending` 时拒绝上传和自动轮询。执行中断时，带阻塞标记的 upload/poll 任务被收成「待核实：交付执行中断」，不重新入队。

## 审查范围和局限

材料是 `review-input.md` 全文（至第 11304 行），包括相对 `d544eae` 的差异，以及诊断、片尾、错误提示、Hook 预览、导入、素材版本、模型、编排、进度、发布、渲染、`app.js`、工作流和所列测试。只读，未改代码，未跑测试；材料里的测试只用来对照分支，不能代替执行结果。

`qc.inspect_output` 的函数体不在材料里。`orchestrator.execute_batch` 传入的期望时长来自 `render_plan['expected_duration_ms']`，这个值已含片尾；同时传入的 `timeline` 仍是请求时间线，不含片尾。渲染器在返回前已用含片尾的时长做过偏差检查。不能从材料判断质检函数是否忽略第二个参数、改从时间线重算。

`encoder_threads` 不在本次 `production.json` 差异里，材料中的并发配置块也没有这个字段，因此不能在这份材料里看到它的当前值。浏览器限额是进程内字典；材料只显示发布单元启动名为 `web` 的服务，不能证明多进程部署下的合计额度。

## 修复及补充复审

上次结论里质检函数体不在材料中。我只读 `supplement.md`，核对这一点后再给复审结论。补充材料只有 774 行。进度条修复已经看到，接下来读质检和发布配置这两段。**通过。** 补充材料里的三处都已核对，没有新的已证实问题。上次的进度条缺陷已消失，质检时长缺口已闭合。

进度条已接上片尾阶段。`app.js` 的 `productionProgress` 在「字幕合成」和「质检」之间加入了 `['静态片尾', ['ending_overlay']]`。阶段为 `ending_overlay` 且仍在制作时，`findIndex` 命中这一组：该步为 `current` 并带 `aria-current="step"`，前面的「字幕合成」为 `reached`，后面的「质检」未开始。`test_execution_events.py` 的 `test_production_cards_distinguish_running_queue_pause_and_finished` 对这三处文案做了断言。

质检使用调用方传入的期望时长，不从时间线重算。`qc.inspect_output` 把容器时长与参数 `expected_duration_ms` 比较，超出 `duration_tolerance_ms` 才记 `duration=… expected=…`。上轮调用点传入的这个值已经包含片尾。时间线只交给 `_sample_title_card`，用来判断成片开头约 100ms 处是否为字卡；片尾在成片末尾，不改变这次抽样，也不把片尾当成字卡。

发布适配器只改预览并发，并在健康检查失败时收回。`engine_with_config` 在 `apply` 里先核对 `current.json` 仍是预期版本，且 `data/.web-state/maintenance.json` 已存在，然后要求 `concurrency.hook_previews` 仍为 1。它把整份配置写入 `history/<目标版本>-runtime-before.json`，再深拷贝后只把该字段写成 3。同一引擎实例再次以非目标版本进入 `apply` 时，写回内存中的原配置。测试在失败路径上期望抛出含「恢复」的错误，并断言运行配置回到 `hook_previews: 1`、当前版本仍是旧版本、备份与改前配置一致。

本轮只读了 `supplement.md`。真实 `release_manager.transition` 的源码不在这份材料里；恢复行为是按适配器代码和所附测试双重核对的。