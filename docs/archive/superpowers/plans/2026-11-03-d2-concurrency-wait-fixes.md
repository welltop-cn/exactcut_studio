# D2：三项并发等待缺陷最小修复方案

> 历史归档（2026-11-04）。正文保留当时的方案或执行状态，不代表当前版本；日常操作见[文档入口](../../../README.md)。

目标：已接收的取帧/源索引工作不因执行资源暂忙转成失败；已有成片播放不在HTTP线程等待后台传输名额。用户已授权在前两项收尾后实施，父review后才更新dev。

基线68713f89b1889295e18badff741350be45b8d2f6；D2既有树 ~/.paseo/worktrees/1kps4qs6/d2-mosaic-active-frame-blur 可继续唯一writer，禁止改当前运行的cut-mosaic-18883-source-delivery树。无需再建多余worktree。Python使用 ~/miniconda3/envs/py3.12/bin/python3.12。

## 约束和已核实事实

只修三项，各自提交和对应回归。不得重写调度器/新增统一平台、调整资源数量、删锁、无限线程、删测试断言、改产品文案。真实FBBM/OSS上传与发布禁止；生产OSS路径用本地HTTP+受控传输替身验证，禁止切真实production配置或访问凭据。原有用户数据不改，18883任务不能取消。先读source/AGENTS.md、服务清单、相关runbooks和原验收协议。

父独立复现脚本 /tmp/parent_review_concurrency_findings.py 仅为定位，不是完整验收：真实同缓存锁占用2秒触发ResourceBusy；源索引进入media.run_checked时仍持身份锁，媒体超时被包装成正片错误；预览传输名额占满时_ensure_uploaded线程等待，释放才继续，没有真实外网。外部审查 /var/folders/mb/7rs6fvr50gvc8yc93d73bvn00000gp/T/grok-matao/grok-review-c209cb1f.md 可参考但不要盲从：source_frames获取身份锁超时在try外，不会由第75行包装，媒体等待异常则会。

## 1. 取帧缓存竞争：已排队工作不能2秒失败

入口：cutting.frame_lease、cutting/mosaic_session.frame_file、editor_frames.run/handle、mosaic_locate；queue.execute对ResourceBusy目前走失败。

- 先写实际HTTP/dispatcher回归：同session两个不同窗口/帧，第一任务真实持cache锁超过2秒；第二已202，随后必须继续等待并在释放后完成JPEG，不能failed/400。用事件/受控阻塞维持竞争，不盲sleep等时间。
- 区分同步HTTP快速路径和已入队worker。后台等待须可取消，沿现有processing_cancel.scope.check，等待期间不放弃同cache单写约束。优先利用现有ResourceDeferred/调度回队或可取消的后台锁等待；选择符合现有调用关系的最小改法，不让同cache等待占完无关任务执行槽。
- 识别/定位内部frame_file也必须覆盖，不只修改editor_frames单点。缓存命中仍立即返回，真正解码失败仍failed，不能把所有错误都重试。
- 验证：同key串行、异key可推进、取消等待释放、维护/退出不死锁、定位竞争不误失败、显式失败后重试契约不破坏。

## 2. 源索引：等待资源不伪装成源片损坏

入口source_frames.prepare、resource_locks.hold、media.run_checked、source_prefetch既有任务恢复/异常分支。

- 先复现两项目同一原片索引：A遇媒体名额占满；B请求同key；持续等待后均可用同一份真实索引，不计episode失败。缩短注入等待期限而非真实等180/1800秒。ffprobe真实解码保留一个集成用例。
- 不要在索引身份锁内长等媒体额度。利用现有任务defer路径/非阻塞资源探测，再获取正确的单资源锁与二次缓存检查；明确锁顺序，不能反过来让大量同key等待者占完media槽。先梳理source_prefetch异常处理，确保ResourceDeferred能够上抛到既有调度层而非被宽泛except转换为failed。
- 同源去重与跨项目索引复用、原source signature验证、原子发布保留。身份锁忙和媒体忙是暂态；损坏视频/不支持时间戳/真实ffprobe失败仍按既有错误处理，不永久重试坏数据。
- 验证取消/维护/重启恢复、同key最终仅一次probe、异key并行、媒体忙报告仍等待、坏源真实失败、source替换不采用旧索引。

## 3. OSS已有成片播放：不得占HTTP线程等worker槽

入口webserver实际预览路由、preview_oss.signed_preview/_ensure_uploaded、editor_resources.transfer。

- 首先建立实际Handler HTTP回归，受控桶替身禁止任何外网；占满transfer（模拟冷编辑器复制），已有本地成片GET必须及时回应且可播放/获取明确既有协议状态，不等1800秒，不返回误导完成。
- 已有有效OSS receipt保持快速签名路径。需要新OSS准备时优先评估已有本地stream_video/Range是否可按既有鉴权与部署契约直接服务，避免播放依赖上传；若当前架构不能安全本地回退，使用现有异步协议或快速暂态响应，先向父报告具体接口选择。不得新建通用上传平台或默认关闭生产OSS功能。
- 禁止在HTTP请求内持transfer等待或同对象锁长等待；后台上传若仍需要，保留实际上传执行额度、跨进程同对象去重、身份复核、私有ACL和安全路径。不得绕过鉴权、回退到公开对象、移除上传校验。
- 测试receipt命中/未命中/身份变化、transfer忙、同对象竞争、真实上传错误替身、Range/权限、响应延时有界、请求取消不留下后台无限线程。明确OSS路径不是FBBM，二者均不能真实外写验收。

## 执行与review

1. 保持一项一提交：红灯→最小实现→绿灯→提交。任何新增协议/模块或范围超上述文件链先向父说明理由，不擅扩。
2. 每项给父：可执行红绿用例、具体用户行为、diff、等待/取消/恢复结果。父独立review与关键复跑。
3. 三项固定候选后完整回归一次，保留原始失败，不复用68713旧结果。
4. 以隔离actual main+真实文件+受控资源占用完成等待场景；OSS网络必须替身。无需重跑20个项目和付费模型，不降低原并发配置。
5. 父review通过后再按空闲屏障、备份、正常停止/切换加载dev；保留worker1/AI1/fetch0/external0/publish0/OSSfalse及用户数据。
6. 18883只做相关真实取帧/索引/本地播放校验，报告明确生产OSS仍为隔离HTTP替身验证。原A完整成功证据保留，不需反复长片重跑。
7. 最终核对health/实际版本/无自有任务泄漏/原文件SHA/零真实上传发布。后续无关问题仅登记。
