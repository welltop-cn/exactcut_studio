# 10 月 30 日发布后 main 可达提交清单

基线：5b0e408（10 月 30 日最后的记录提交）；终点：59acccb。按 Git 祖先范围取全集，包含在此期间合入、但更早编写的分支提交；合并提交单列，不重复当成 bug。

统计：{"product": 111, "docs/other": 40, "merge": 32, "test/tool": 15}

| 日期 | 提交 | 类型 | 说明 |
| --- | --- | --- | --- |
| 2026-10-31 | `bc37c09` | product | feat: persist cut and mosaic jobs in a fair background queue |
| 2026-10-31 | `a1c269f` | product | fix: open subtitle timeline only on explicit user action |
| 2026-10-31 | `bf99db9` | product | fix: revalidate mosaic replacement under the asset gate |
| 2026-10-31 | `e9b03a3` | product | fix: correlate mosaic submission recovery and preserve rejected edits |
| 2026-10-31 | `2c6a71d` | docs/other | docs: record mosaic queue parent review fixes and validation |
| 2026-10-31 | `bfeab19` | docs/other | docs: record independent cut mosaic queue review |
| 2026-10-31 | `8216c27` | product | fix: recognize only the lower subtitle region before Gemini input |
| 2026-10-31 | `58451fd` | merge | Merge validated bottom-only subtitle production fix |
| 2026-10-31 | `4363673` | docs/other | docs: record bottom-only subtitle production fix and recovery |
| 2026-11-02 | `1a8d7c0` | docs/other | docs: record live four-material mosaic queue user trial |
| 2026-11-02 | `5980929` | product | fix: show actionable validation messages and locate ending settings |
| 2026-11-02 | `3f2ef04` | product | fix: keep technical error details out of primary user messages |
| 2026-11-02 | `504359c` | product | Separate bounded CPU editor resources from final mosaic jobs |
| 2026-11-02 | `10e4782` | product | Synchronize state transfers only in local MPS SAM workers |
| 2026-11-02 | `aae07df` | docs/other | Record CPU smoke and bounded local concurrency verification |
| 2026-11-02 | `c93dc49` | docs/other | Record parent approval and controlled local trial upgrade |
| 2026-11-02 | `9b4b2bb` | docs/other | Record verified local trial upgrade with preserved user state |
| 2026-11-02 | `9860f90` | product | Remove application budgets from Gemini and intake parsing |
| 2026-11-02 | `fa42015` | product | Bound media stages with inherited identity-based leases |
| 2026-11-02 | `abd089f` | product | Persist interactive admission and dispatch bounded background work |
| 2026-11-03 | `cfc8114` | docs/other | Record fixed-candidate regression and isolated dev acceptance |
| 2026-11-03 | `81d66ac` | product | Start interactive dispatcher independently of the ordinary worker |
| 2026-11-03 | `96c1aa5` | product | Bind Hook receipts to current video and shot-index identities |
| 2026-11-03 | `e2cbcc0` | docs/other | Record parent-review fixes and worker-disabled dev revalidation |
| 2026-11-03 | `722b84d` | docs/other | Record authorized idle-safe local18883 resource update |
| 2026-11-03 | `05b2faf` | product | Serve existing local originals in development without remote fetch permission |
| 2026-11-03 | `bbfa4f7` | test/tool | Initialize tracking capability before opening mosaic test workspace |
| 2026-11-03 | `6ab0245` | docs/other | docs: record actual 18883 acceptance and deferred final timeout |
| 2026-11-03 | `d47d7f9` | product | Drop the local originals notice added with local source serving |
| 2026-11-03 | `68713f8` | product | Enable mosaic gblur only on source frames that can carry a mask |
| 2026-11-03 | `afae711` | product | Keep accepted frame and locate work queued when the cache lock is busy |
| 2026-11-03 | `169f618` | product | Defer source indexing instead of holding its identity lock across media wait |
| 2026-11-03 | `7cf9679` | product | Keep existing result playback off transfer and object-lock waits |
| 2026-11-03 | `f34aabd` | product | Make OSS playback validate receipts only, never upload in the request |
| 2026-11-03 | `5a64c35` | product | Prepare missing OSS previews in the background, never on the HTTP thread |
| 2026-11-03 | `3c01f73` | product | Wait for OSS download readiness before triggering the browser download |
| 2026-11-03 | `b59de96` | product | Record authorized AI budget 60/5 and completed local service changes |
| 2026-11-03 | `7ab6e16` | docs/other | Preserve approved concurrency plans and separate upload design research |
| 2026-11-03 | `1bcb609` | merge | Merge verified background concurrency fixes preserving main UI safeguards |
| 2026-11-03 | `126d840` | merge | Integrate authorized main AI limits consistently at 60 RPM and burst 5 |
| 2026-11-03 | `fc05ac3` | docs/other | docs: replace local absolute paths and Mac Tailscale address before publishing |
| 2026-11-03 | `5dc30a2` | product | Split tests into unit/ui/integration layers with xdist-safe isolation |
| 2026-11-03 | `4763ccd` | test/tool | Add devtools/test_layers: probe, classify and apply explicit pytest layer marks |
| 2026-11-03 | `6937d07` | product | Split dependency locks: runtime-only for production, dev tools separately |
| 2026-11-03 | `4facec8` | product | Release build runs the full test suite (-o addopts=), matching linux_ops |
| 2026-11-03 | `e1d03ab` | merge | Merge build/split-dev-deps: test layering (unit/ui/integration) and runtime/dev dependency lock split |
| 2026-11-03 | `0813e0d` | product | fix: legacy pause/archive no longer 409 while a production stage holds the media gate |
| 2026-11-03 | `c8e8bef` | product | Production children no longer take shared editor media/frame/transfer slots |
| 2026-11-03 | `86127bd` | merge | Merge fix/production-own-slots: production ffmpeg/ffprobe/upload children stop taking the shared cut/mosaic media, frame and transfer slots; pause freezes production ffmpeg again |
| 2026-11-03 | `0ab1b57` | merge | Merge fix/legacy-pause-during-production: legacy pause/archive no longer return 409 while a production stage is running |
| 2026-11-03 | `4ea4326` | product | Restore 识别未完成，请重试 for failed cutting scans; skip unreachable helper test |
| 2026-11-03 | `ddf21bb` | merge | Merge fix/error-surface-tests: restore scan failure message, skip hidden helper test |
| 2026-11-03 | `ef5f793` | product | Short media work takes no execution slot (limiter audit S1+S2) |
| 2026-11-03 | `9444832` | product | Hook previews and locate get their own interactive roles (limiter audit S3) |
| 2026-11-04 | `021d2fa` | product | Interactive claims no longer pre-reserve later-stage slots (limiter audit S4) |
| 2026-11-04 | `ed4652a` | product | Drop the shared transfer slot; keep the per-object OSS lock (limiter audit S5) |
| 2026-11-04 | `af9dbbb` | product | Final cut/mosaic renders are limited only by the queue (limiter audit S6) |
| 2026-11-04 | `2dad1e1` | product | Slot waits in cancellable workers no longer fail at 1800s; SAM timeout by frames (limiter audit S7) |
| 2026-11-04 | `b90fb4c` | product | Outbox recovery reads the job list once per scheduler tick |
| 2026-11-04 | `b9e3f15` | merge | Merge origin/fix/short-ops-no-slots-v2 into test/concurrency-8 |
| 2026-11-04 | `2b575a3` | merge | Merge origin/fix/hook-preview-own-slots into test/concurrency-8 |
| 2026-11-04 | `632b696` | merge | Merge origin/fix/no-preclaim-slots into test/concurrency-8 |
| 2026-11-04 | `c4c1c09` | merge | Merge origin/fix/no-transfer-slots into test/concurrency-8 |
| 2026-11-04 | `5e989e0` | merge | Merge origin/fix/final-queue-only into test/concurrency-8 |
| 2026-11-04 | `5914029` | merge | Merge origin/fix/no-wait-timeout into test/concurrency-8 |
| 2026-11-04 | `e490c7f` | merge | Merge origin/fix/scheduler-read-once into test/concurrency-8 |
| 2026-11-04 | `c8f5768` | test/tool | test: drop removed transfer role from short-ops slot fixture after merging no-transfer-slots |
| 2026-11-04 | `8061f2d` | product | Keep claimed final jobs queued until a final media slot frees (S6) |
| 2026-11-04 | `2919583` | docs/other | docs: handoff for concurrency-8 testing (progress and open issues) |
| 2026-11-04 | `36fe8db` | docs/other | docs: record Mac takeover verification and reproducible pause failures |
| 2026-11-04 | `6d538c2` | product | fix: exclude batch suspension from media command timeouts |
| 2026-11-04 | `58eea96` | product | fix: allow failed candidate executors to exit and release slots |
| 2026-11-04 | `098ffad` | product | fix: honor managed render backoff before replacing its executor |
| 2026-11-04 | `df39e8e` | docs/other | docs: record two-Mac concurrency acceptance and three core fixes |
| 2026-11-04 | `63c6d04` | test/tool | test: align hook capacity and await mosaic initialization before assertions |
| 2026-11-04 | `38a5ea5` | docs/other | docs: record main merge readiness and accepted test-only failures |
| 2026-11-04 | `9002646` | merge | Merge concurrency scheduling updates and pause/retry fixes |
| 2026-11-04 | `0b35e91` | docs/other | docs: consolidate current guides and archive historical records |
| 2026-11-04 | `f6a084e` | product | feat: integrate compact mosaic timeline and fixed-region live preview |
| 2026-11-04 | `4c1b1ec` | product | fix: resume prefetch resource waits after draft plan edits |
| 2026-11-04 | `a2cc9bc` | product | feat: mark deleted spans directly on timeline thumbnails |
| 2026-11-04 | `838ecf3` | product | fix: preserve legacy prefetch resource-wait recovery |
| 2026-11-04 | `e0aea25` | product | feat: fill timeline thumbnails and preview deleted spans immediately |
| 2026-11-04 | `0319e1d` | product | fix: simplify clip editing and suppress truncated deletion labels |
| 2026-11-04 | `f1e2260` | product | wip: hand off fixed-only mosaic simplification and mask performance work |
| 2026-11-04 | `ddaf4e4` | product | fix(mosaic): convert legacy drafts to fixed visibly; never replace an old tracked result |
| 2026-11-04 | `5143476` | test/tool | test(mosaic): align UI tests with fixed-only mode |
| 2026-11-04 | `bf92536` | test/tool | test(mosaic): prepare_mask full output equals the old MaxFilter pipeline for every style/expansion |
| 2026-11-04 | `c85898b` | docs/other | docs(mosaic): record fixed-only takeover tests, legacy-draft decision and real-footage benchmark |
| 2026-11-04 | `89756dd` | product | perf(cutting): read packet timestamps instead of decoding every frame in probe_source |
| 2026-11-04 | `82fb372` | product | perf(cutting,mosaic): re-encode only cut-point and masked GOPs, stream-copy the rest |
| 2026-11-04 | `64d2dd6` | product | perf(segments): one-frame encoder trial rejects non-continuable sources before any piece encode |
| 2026-11-04 | `e0fbb9c` | docs/other | docs: segment re-encode verification, real-footage timings and quality |
| 2026-11-04 | `726f088` | product | feat(cut-editor): open shows the video and only reads scan records; whole-video scan runs on click |
| 2026-11-04 | `7cb31d3` | product | feat(mosaic-editor): stop auto-submitting the whole-video scan on open |
| 2026-11-05 | `33b1b20` | product | feat(mosaic-editor): regions apply on release and stay editable while stepping |
| 2026-11-05 | `4abd08c` | product | feat(mosaic-editor): show every clip on the timeline and outline unadopted regions |
| 2026-11-05 | `69266c9` | product | feat(mosaic-editor): delete/mosaic/ignore per clip, timeline drag-to-add, auto-applied times |
| 2026-11-05 | `6eb5cbc` | product | fix(mosaic-editor): toggle-off, space shortcut, m:s input and test fixes |
| 2026-11-05 | `ea33d05` | product | feat(mosaic-editor): always-visible clip summary in the editor header |
| 2026-11-05 | `8007d82` | product | fix(mosaic-editor): timeline block tooltips, min width, and latest jump wins over stale trim seeks |
| 2026-11-05 | `4990135` | product | fix(mosaic-editor): keep full editor draft across discarded renders; confirm before rendering with pending clips |
| 2026-11-05 | `ee12519` | test/tool | test(mosaic-ui): accept pending-clip confirm in render tests |
| 2026-11-05 | `dd0a2f3` | product | fix(mosaic-editor): show plain recognition-unavailable message instead of source-missing guidance |
| 2026-11-05 | `c97ca1c` | product | fix(mosaic-editor): neutral empty-state on open; only this-session empty scan says none found; header says 还没有片段 |
| 2026-11-05 | `645940b` | product | feat(mosaic-editor): dragging empty timeline scrubs the playhead instead of creating clips |
| 2026-11-05 | `31ca934` | merge | Merge branch 'docs/consolidate-current-guides' |
| 2026-11-05 | `d9a5148` | merge | Merge prefetch resource recovery and keep verification in the archive |
| 2026-11-05 | `7562fe6` | merge | Merge origin/feat/editor-pick-p5p7 into integrate/main-20261105 |
| 2026-11-05 | `ff641d3` | product | fix: same-page hash deep links (origin/fix/hash-deeplink b5b9599) |
| 2026-11-05 | `e8ca44b` | product | fix: explicit preview upload retries (origin/fix/preview-upload-retry 38a88ac) |
| 2026-11-05 | `b537787` | product | fix: relocated media and mosaic diagnostics (origin/fix/media-path-relocate fb91413, 566ee41) |
| 2026-11-05 | `2e23152` | product | fix: release render locks before confirm (origin/fix/mosaic-confirm-race 40fbbde) |
| 2026-11-05 | `c2e46b9` | product | fix: friendly recognition errors (origin/fix/public-error-surfaces-ui f380417, 89192a8) |
| 2026-11-05 | `99d82a8` | product | fix: pause guards and archive cancellation (origin/feat/pause-archive-cancel-cutting 33fa6a1, 98f10d3) |
| 2026-11-05 | `271591f` | product | fix: enforce CPU-only SAM per integration policy |
| 2026-11-05 | `9d5240e` | docs/other | docs: redact machine paths and public addresses from imported archives |
| 2026-11-05 | `79af463` | test/tool | test: provide browser event context for startup regression |
| 2026-11-05 | `0b3ff78` | product | fix: preserve edited output paths in standalone CLI projects |
| 2026-11-05 | `cffdb1f` | test/tool | test: model hash listener in workspace restore fixture |
| 2026-11-05 | `06f4a66` | product | 修复分段重编码中短编码片段引起的 DTS 节奏异常 |
| 2026-11-05 | `a306035` | product | 为剪切和打码渲染事件添加任务号上下文 |
| 2026-11-05 | `fc40757` | product | 新增线上监控脚本：服务存活、资源、报错事件推送飞书 |
| 2026-11-05 | `f1cab63` | docs/other | 记录线上监控上线：变更索引与独立记录 |
| 2026-11-05 | `a34ca5d` | product | 新增 root 日志复制脚本：增量复制 error/critical 行供 deploy 监控读取 |
| 2026-11-05 | `b92d2b5` | docs/other | 预写 root 日志复制变更记录，补记 ACL 未生效原因 |
| 2026-11-05 | `0c70084` | docs/other | 补记 root 日志复制执行时间与验证结果 |
| 2026-11-05 | `a48d93d` | product | fix(diagnostics): 追加日志时不再 chmod，避免清掉 ACL |
| 2026-11-05 | `5c8b899` | merge | 合并 fix/diagnostics-keep-acl：追加诊断日志时不再 chmod，保留 ACL |
| 2026-11-05 | `cb94673` | merge | 合并 feat/prod-monitor：线上监控脚本与 root 日志复制脚本 |
| 2026-11-05 | `a8a6bff` | product | 改进原片预取进度提示，并修复重复 session cookie 登录 |
| 2026-11-05 | `cb3aaf0` | product | 打码时间轴缩略图改用服务端精灵图并按 sha 落盘缓存 |
| 2026-11-06 | `b5812ab` | product | 修复项目页剪切/打码进行中仍显示成片已就绪 |
| 2026-11-06 | `5fd750c` | product | 告警补充项目深链与排障上下文 |
| 2026-11-06 | `91ea384` | docs/other | 记录线上监控告警上下文脚本部署 |
| 2026-11-06 | `b66a8ba` | product | 告警补充项目深链与排障上下文 |
| 2026-11-06 | `4505c48` | product | 补齐报错事件项目与请求上下文并脱敏展示上游地址 |
| 2026-11-06 | `d8c1d07` | merge | 合并 fix/project-page-cutting-status：项目页剪切/打码进行中状态 |
| 2026-11-06 | `6c6f074` | merge | 合并 fix/prod-monitor-context：告警上下文与线上部署记录 |
| 2026-11-06 | `cfd09b3` | merge | 合并 feat/event-log-context：报错事件补项目与请求上下文 |
| 2026-11-06 | `14150ec` | merge | 合并 feat/timeline-filmstrip：打码时间轴服务端精灵图与 sha 缓存 |
| 2026-11-06 | `15c3353` | merge | 合并 fix/source-prefetch-progress-ui：原片预取进度提示与 session cookie 修复 |
| 2026-11-06 | `489c5dd` | product | 修复音频尾部较长导致原片完整性校验误判 |
| 2026-11-06 | `3507c44` | product | 修复打码时间范围纠正后残留旧校验错误 |
| 2026-11-06 | `0031322` | product | 完善打码片段勾选、时间调整与区域编辑交互 |
| 2026-11-06 | `2554630` | docs/other | 补充本地验收环境说明与生产版本差异检查清单 |
| 2026-11-07 | `ee59ade` | docs/other | 记录上线候选整合核对及双平台回归阻塞 |
| 2026-11-07 | `b50b3cc` | test/tool | 同步草稿恢复及片段编辑回归与现有交互约定 |
| 2026-11-07 | `824cfb5` | product | 限定分段画质校验帧范围并补充损坏回退回归 |
| 2026-11-07 | `1a7b4be` | docs/other | 记录发布阻塞修复及双平台完整验收结果 |
| 2026-11-07 | `dbe98bb` | docs/other | 记录主分支正式上线及生产核验结果 |
| 2026-11-07 | `a58a672` | product | 优化首屏项目加载链路并解除只读工作台恢复的全局锁等待 |
| 2026-11-07 | `f40e59f` | docs/other | 记录生产真实浏览器验收及测试OSS清理结果 |
| 2026-11-07 | `4bdde9d` | docs/other | 记录首屏优化正式上线及真实浏览器性能验收 |
| 2026-11-07 | `4dfe8a4` | docs/other | 归档近期线上报错并用原故障数据复验修复结果 |
| 2026-11-07 | `faf29bc` | product | 修复原片流传中断续传、纳管项目结束任务抛错与发布修订冲突不自愈 |
| 2026-11-07 | `4a5bc20` | product | 修正续传时误关重连流 |
| 2026-11-07 | `82e4c7a` | docs/other | 记录原片续传、纳管任务结束与发布冲突修复并上线 |
| 2026-11-07 | `2fd1016` | product | 打码编辑器：拖动竖轴直接用播放器画面，不再排队后端准确帧 |
| 2026-11-07 | `c631b03` | merge | 合并 fix/mosaic-local-frame：打码时间轴拖动改用播放器本地帧 |
| 2026-11-07 | `79b2ca8` | test/tool | 补齐时间轴回调测试中的预览等待定时器夹具 |
| 2026-11-07 | `1c121f4` | docs/other | 记录打码时间轴本地预览正式发布与回归结果 |
| 2026-11-08 | `0bb12d4` | test/tool | 补充调度器行为固定回归测试（worker_tick 优化前基线） |
| 2026-11-08 | `72316e8` | product | B0：给 web/batch_control 两把锁和 worker_tick 加分段计时 |
| 2026-11-08 | `91b3de9` | product | B1：同一 worker_tick 内复用父项目 state、内部子项目身份和 job 解析结果 |
| 2026-11-08 | `c5c5c59` | product | 详情接口 batch_response：同一次请求内按 batch_id 记忆父项目映射 |
| 2026-11-08 | `317a181` | product | B0 小修：worker 持锁事件的 owner 统一记为 worker.tick |
| 2026-11-08 | `5c740b6` | test/tool | 修正 test_lock_timing 中 lock.summary 断言的线程竞态 |
| 2026-11-08 | `01f1b4e` | test/tool | 测试：新增跳过已结束优化的等价性基线（稳态、四种注入、补建后继、多账号接口快照） |
| 2026-11-08 | `df20879` | product | 性能：worker 跳过已结束项目的重复检查（输入指纹不变且上一轮无动作才跳过，首轮全量、每5分钟兜底、EXACTCUT_SKIP_FINISHED=0 可关闭） |
| 2026-11-08 | `1b76fb9` | product | 性能：worker 单轮内复用实例标记和任务列表，减少重复扫描 |
| 2026-11-08 | `309a56d` | product | 批量缓冲前端诊断事件并兼容逐条日志校验 |
| 2026-11-08 | `d28c338` | product | 切换项目期间推迟轮询并跳过刚读取的项目详情 |
| 2026-11-08 | `9a15b1d` | product | 切项目后 150ms 内查询原片和预取素材，并取消过期请求 |
| 2026-11-08 | `c1c1710` | product | 修复素材下载中反复请求 sources 重复建预取任务 |
| 2026-11-08 | `63d7e86` | product | 修复坏任务隔离避免阻塞其他项目调度 |
| 2026-11-08 | `8e72af8` | merge | 合并已结束项目跳过与单轮复用性能优化 |
| 2026-11-08 | `5ce21c3` | merge | 合并前端切项目请求链路优化 |
| 2026-11-08 | `55b927d` | merge | 合并素材预取任务去重修复 |
| 2026-11-08 | `4713ffe` | merge | 合并坏任务隔离修复并保留调度快照 |
| 2026-11-08 | `692b0f7` | product | 修复坏任务导致列表详情读取失败并补齐合并回归 |
| 2026-11-08 | `e31abe7` | product | 修复收割任务对象脱节并适配前端回归测试 |
| 2026-11-08 | `de8d7e7` | product | 修复纳管与旧项目并发预取的锁顺序死锁 |
| 2026-11-08 | `6beaa44` | docs/other | 记录主线差异审查、预取死锁修复与正式发布结果 |
| 2026-11-08 | `d452608` | docs/other | 记录线上 nginx 文本压缩配置与响应核验结果 |
| 2026-11-08 | `de69bc2` | product | 修复队列只读扫描竞争调度全局锁 |
| 2026-11-08 | `e68c589` | product | 修复打码区域框在播放线离开所选片段后仍显示的问题 |
| 2026-11-08 | `15f7155` | merge | 合并打码片段外隐藏区域框修复 |
| 2026-11-08 | `aee6fe9` | merge | 合并队列只读扫描免全局锁修复 |
| 2026-11-08 | `868f570` | docs/other | 记录晚间修复合入主线、全量对照与正式发布结果 |
| 2026-11-10 | `a4cfc28` | product | 暂存上传冲突受控同步修复供独立会话接续 |
| 2026-11-10 | `5919cc8` | product | 修复上传修订冲突与提交回执状态同步 |
| 2026-11-10 | `7f1119c` | test/tool | 测试草稿：记录 Hook 异步预览置脏回归边界（待完成） |
| 2026-11-10 | `8eedea1` | product | 修复 Hook 预览误改草稿并冻结确认版本的有效预览 |
| 2026-11-10 | `2578502` | product | 避免确认制作持锁读取整片原片并跳过损坏预览数据 |
| 2026-11-10 | `440bd37` | docs/other | 记录预览与上传修复的父级审查和组合验收 |
| 2026-11-10 | `e53c40b` | docs/other | 记录预览与连续上传修复的本地浏览器验收 |
| 2026-11-10 | `59acccb` | docs/other | 记录预览与上传修复上线及生产验收和对象清理 |
