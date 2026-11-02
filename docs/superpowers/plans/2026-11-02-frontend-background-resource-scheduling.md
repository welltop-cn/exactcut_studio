# 前台接收、后台受控执行：实施分步与本地验收

基线9b4b2bb；隔离树`cut-mosaic-resource-scheduling`，分支`feat/frontend-background-resource-scheduling`。唯一业务依据：[统一方案](/Users/matao/code/exact-cut-fbbm-handoff/docs/design/frontend-background-task-policy.md)；[调查附件](/Users/matao/code/exact-cut-fbbm-handoff/docs/design/interactive-admission-resource-scheduling.md)只作路径证据。18883保持PID59032和原interactive-resources代码/数据，不热改、不部署生产。

## 固定候选执行结果

基础源码固定为`abd089fda3f2b60ed66e8d2359348d2f6860bd8b`，分为`9860f90`、`fa42015`、`abd089f`三个关注点提交。基础全量2049 passed/1 skipped/10既有警告（660.13s）及模型混合dev属于该版本。父review后只补`81d66ac`普通worker0的交互生命周期和`96c1aa5`Hook源/索引回执有效性；新固定源码为96c1aa5，已通过实际worker0 development补验，并完成新全量2057 passed/1 skipped/10既有警告（673.17s）。

实际main入口核心与provider明确替身的边界矩阵均已完成；详见[固定候选验收](../../verification/frontend-background-resource-scheduling-2026-11-02.md)及JSON。真实场景观察到普通FFmpeg3、final媒体1与两用户实际SAM产出同时执行；字幕实际HTTP2+30/第33拒绝、四路文本Excel、Hook/帧7个pending维护重启恢复、12普通接收也已核对。核心验收脚本的错误SHA和legacy结果路径失败保留，未修改应用校验或结果记录。父最终review通过后，18883已在实际空闲、维护屏障与备份回退准备下按明确授权于2026-11-03正常更新；未发布生产或合并main，未取消用户任务，详情见[升级记录](../../verification/frontend-background-user-upgrade-2026-11-03.md)。

## 旧产物处理

旧unlimited-interactions树及096b539、0ede965和未提交媒体改动全部保留，不整批合入。逐项复用Gemini/文本Excel脱离原RPM、解析网络移出全局mutation事务、助手入口隐藏、字幕30、去普通10门槛、按真实key保护缓存。直接无限起线程和相关“同时全部进入”的旧测试不作为本方案。

已退出旧自有测试进程，无遗留真实验证服务；未停止用户18883。旧真实CPU/Gemini小规模证据保留，只作已有路径证据，不当本方案验收。旧媒体父死亡红灯表明media.run_checked未继承新resource lease；只有接入资源租约时所必需的继承修复纳入本轮。

## 最小实施分步

1. 复用与并发直接相关的兼容改动：Gemini/文本Excel预算、解析短事务、助手隐藏、字幕2+30、普通提交去10。保留其他OpenAI预算和真实供应商错误。
2. 补一个交互持久等待队列，适配现有scan/locate/track/clip worker和Hook；不重写普通/字幕/final队列。HTTP只登记报告与描述符，返回queued或复用状态；dispatcher按owner轮转，资源不足保留pending，不为每项创建等待线程。保留已有worker/batch/cache FD和cancel语义。
3. 沿现有editor_resources租约集中执行额度：去掉每账号接收槽，把执行租约移到dispatcher。媒体阶段结束即释放；短准确帧有独立执行资源与真实cache key锁；SAM child作为复合阶段，避免父子重复计费。全片索引/OSS保持对应后台阶段与真实key去重，不改无关数据模型。
4. 前端识别/预览/Hook消费queued/running/terminal状态，允许继续编辑、离开恢复；结果绑定操作/输入版本，迟到任务不覆盖新设置。final提交冻结及显式确认替换完全保留。
5. 本地dev使用候选共享业务配置，完成入口级验收，再一次稳定候选全量及父review。没有生产上传/发布；不做极限容量或MPS研究。

## 资源profile（已有代表场景功能证据，不是生产容量核定）

- 确定业务并发：普通fetch2/render3/upload2/poll4，无额外总1（推导11）；final mosaic1；字幕2运行+30等待。
- 新交互执行起点沿已有有限组合：识别worker2、预览worker2，不限接收数/账号；SAM由final1+交互预览2构成可观测复合阶段。HTTP不预占这些槽。
- 本地长媒体阶段集中候选为共享5+最终专用1（总6），短媒体候选2；共享5对应普通3+交互2，最终专用1避免被共享池持续挤占。总6仅是混合验证起点，所有FFmpeg/SAM路径核实后明确实际覆盖，不宣称Linux长片容量。云请求不持已完成的媒体阶段资源。
- FFmpeg普通线程默认4、交互encoder2、SAM线程4沿既有配置/代码核实并在候选配置及本地测试共同核对，不借设备差异隐藏线程/业务并发覆盖。
- 只做本次相关参数的effective值比较及偏差失败测试，不建立通用配置平台。默认/生产模板/隔离dev合并后的本次参数须一致；端口、数据、凭据、外部写入和实际CPU/MPS设备差异单独列白名单。模板和历史render3不冒称线上实时runtime。

## 本地验收矩阵（每项记录入口、revision、结果和证据）

|项目|真实入口与关键观察|替身边界|
|---|---|---|
|忙时创建/保存|本地HTTP/浏览器，受控重任务满时创建/编辑/保存与重复提交；处理延迟p95目标<1s|不将实际视频/模型耗时算短请求延迟|
|交互持久接收|同账号3+素材和多账号scan/locate/track/clip/Hook；超执行额度queued，无容量429、无每项等待线程；FIFO/owner轮转|可控provider用于准入和故障；另做适量真实CPU SAM/Gemini|
|A/B/C主流程|A最终不断执行，B真实识别/预览/编辑/入最终队列，开C；两账号SAM实际重叠；最终最大1|小规模CPU证明，不是长片容量认证|
|普通提交/执行|>10、三个旧入口和自动source_fetch；生产profile三路真实FFmpeg与fetch/upload/poll重叠，同lane不超配额、同任务依赖不乱|外部网络步骤本地受控替身，不生产上传|
|字幕|实际入口2active+30pending，第31pending明确未接收；FIFO/取消/重启恢复/缓存|不做32份付费识别|
|限频|Gemini与文本Excel不改耗尽原桶；其他OpenAI仍限流；HTTP解析并发|供应商故障明确替身，真实少量请求另记|
|缓存与资源|同key单生产、异key并行；短帧不等长片；取消/失败/父死亡/重启无孤儿、无提前释放FD|小视频真实FFmpeg/ffprobe；OSS隔离替身|
|前端状态|queued可见，离开恢复，新编辑不被旧结果覆盖，final冻结/确认/放弃竞态|独立浏览器窗口，不动用户tab/localStorage|
|配置|候选有效本次业务参数相等，偏差失败；记录实际CPU进程/内存/等待/临时磁盘|不声称已读取线上runtime|

## 任务契约的边界

新增交互描述符只保存服务器验证过的kind、job身份、actor、batch/session、report相对路径、配置和输入快照/只读permit；不保存模型密钥，不从客户端接受任意路径或runner。已有job报告仍是该业务结果权威；队列只持有pending/active与顺序。schema_version=1，旧报告继续可读。

queued、running、cancelling、completed、failed、cancelled、superseded明确；同用途未开始的旧预览可替换，其他素材不替换。重启时安全本地工作在重新验证输入/权限后可重排；已经发出且结果未知的云请求标记中断，不宣称恰好一次。旧final/subtitle权威状态不复制成新队列。

## 待审批的其他发现

本次之外的问题仅记录于验收交付的待审批清单，不顺手修复。记录问题、证据、影响、建议和为何超出范围。与本次并发租约直接相关的media.run_checked继承FD属于必要安全依赖，须给红绿证据。没有额外生产操作或通用重构。
