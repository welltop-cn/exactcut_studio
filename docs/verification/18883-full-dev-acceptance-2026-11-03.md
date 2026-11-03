# 18883 实际交付验收（2026-11-03）

**结论：部分通过，完整交付未完成。** 原四素材项目的原片列表及四条引用错误已消除；多项目普通制作、真实模型、预览和B最终输出已验证。A的“删除30帧＋打码”最终合成超时，仍为FAIL，等待用户审批是否修复既有长片合成成本。没有以队列`done`、短片诊断或回归总数代替业务成功。

## 固定版本与当前服务

- 实际入口：`http://mosaic-queue.localhost:18883/home/#0d6fdddb468348ad92e0cbd8f5de1db5`。
- 当前PID97825，health为development/ok、worker_alive=true、maintenance=false；应用源码`05b2faf74e37e611f5b0a59065fb199e08786bae`，工作树`cut-mosaic-18883-source-delivery/source`。
- 测试修正提交`bbfa4f7`，在独立`cut-mosaic-18883-verification`树；只将一个UI夹具的tracking能力赋值提前，`source/exactcut`和配置对05b2faf无差异。没有热改当前服务源码。
- 最终设置：worker1、AI1、素材获取0、EXTERNAL_WRITES0、PUBLISH0，OSS预览/外部上传保持关闭；MPS及真实Gemini/SAM模型配置保留。
- 有效并发与**同版本候选生产业务配置**一致：fetch2/render3/upload2/poll4，总11；final1；字幕2active+30pending；preview/scan/preprocess各2、共享media5/final_media1、frame2/transfer2、普通FFmpeg编码4线程/交互编码2线程、SAM4线程；其他OpenAI预算6RPM/burst1。不是线上运行配置检查或Linux最大容量认证。

## 原项目错误：真实红绿与保护

认证GET项目详情200，最初准确返回四条“本集正片尚未就绪”。原片列表GET400，原因是原远端列表入口要求素材获取开关，trace为`4769bdc68e5b4e5987435bd6ce106d70`。worker0另会在入队前拒绝`/sources`，不能混称一个原因。

原trial来源脚本只复制成片、预览、片尾和字幕缓存，没有制作所需的101682 EP1–EP7及帧索引。草稿实际权威内容为纳管状态的`state.draft`/HTTP详情；遗留`draft.json`及`state.document.draft`不能代替当前草稿引用。

用户授权下载后，通过现有`source_cache.ensure → exactcut.dev_fetch`，仅下载现存OSS对象的EP1–EP7；该development适配器仅HEAD/download，缺失不触发远端复制。七集总1,418,033,103字节、各有SHA/实际帧数/时长、独立inode，无hardlink。沿真实`source_frames.prepare`生成七份索引，没有伪造索引或改时间引用。

最终实际GET：原项目`errors=[]`、revision68、四素材保留；本地列表200且七集齐全，原片Range206。自动`diagnose_source_delivery.py --assert-ready`由红转绿。**原项目422个既有文件SHA全部未变**，包含草稿、媒体账本、旧成片及会话/结果。新增源片与可重建索引单独记录，不恢复数据库，不自动确认替换。

## 必要源码差异及边界

05b2faf相对此前并发源码仅增加本地原片修复：

|文件/改动|必要性与边界|
|---|---|
|`originals.py`：development+fetch0读取既有本地cache/project源文件|直接修原400；无片正常空列表，读取不存在返回404；不探测视频、不调用OSS/MetaBase，不增加下载功能，fetch1/production原行为不变|
|本地`listed:null`|上架状态未查询，不能伪造成true或false；沿既有字段增加明确的未知语义|
|新分支notice“仅显示本地正片，上架状态未查询”|说明列表来源，未改原有正常提示语；其显示本身不是消除400的必要条件，若需收回由父/用户另行判断，本轮未二次改文案|
|`app.js`两处三态转换|缓存不把null强转false，渲染仅false显示原“未上架”标记；是契约联动，无全局产品重设计|
|`webserver.py`本地读取、Range及附件名|列表可用后下载也需走本地；保留原项目ACL、剧号/集号校验、共享租约及路径/symlink保护；附件头只用于该本地下载，其他视频流默认行为不变|

没有新增任务类型、队列平台或配置平台。05之后产品源码、交互和文案冻结。bbfa4f7只改测试夹具，不加sleep、不扩大timeout、不删或弱化原三个业务断言。

## 实际18883业务与多项目并发

首先由独立Chromium登录原项目，查看七集列表，复制真实四行配置，编辑副本标题、保存、整页刷新核对，再从页面确认普通制作。源片、时间范围、Hook、品牌和片尾沿原配置；序号/ID按现有复制规则生成，验收标题只标记副本。

|项目|账号/用途|结果|
|---|---|---|
|4044056a2e9b45e48c37563b7b22d55d|mosaic_trial；初始实际浏览器闭环|4条普通QC通过；后续A/B/C编辑与模型、预览及最终输出验证|
|124359d94f0a459f8aa52418a95e7eba|独立验收账号A，A1|4条普通QC通过|
|ec03aa98a94848278692ca63bf86833f|账号A，A2|4条普通QC通过|
|9b1ff66d266e4f7387fa86b1e1f7e410|账号A，A3|4条普通QC通过；只暂停此排队项目8秒，再恢复并完成|
|2425f48d556849ef809e46a9e44b759e|独立验收账号B，B1|4条普通QC通过，跨账号访问另一方404|
|bfce7d1afa654617bed8297fa8899569|账号A，AI1最小补验|1条普通QC通过，真实Hook语义AI|

A1→B1→A2→A3正式接口交错提交，重复同token复用任务。**16条真实素材render描述符全部持久接收，pending/running为16（>10）**；各项目最终4条完成，无此用例内饥饿。暂停的是排队验收项目，没有取消现有用户任务，也不将其当作运行中强制终止测试。

20条AI0输出逐条核对：业务`qc_passed`且QC无错误，文件SHA=结果SHA=QC SHA、1080×1920、帧数对计划最多相差1帧、音视频各一流且时长差<0.2秒、引用七集源SHA未变。没有将历史旧输出当新制作。实际全局render峰值3，**116条采样同时render3/media5**；不是每项目各3变9。实际preview峰2、scan峰2、preprocess峰2、frame峰2、transfer观测峰1；短阶段可能避开采样，不能推断transfer2未工作。

20条沿原AI0环境执行，记录明确为`hook_emphasis.source=isolated_disabled`，只证明实际剪辑、排版、QC及并发，**不冒充生产Hook AI检查通过**。

## 实际模型、前端和预算

- 两条新成片真实Gemini整片识别均completed、风险事件0。没有伪造阳性发现或由此声称风险检出准确率。
- 根据真实取帧人工选定脸部区域，三素材各30帧真实MPS/SAM预览completed、有音频且无告警；另有实际UI预览及双账号相同源/同区预览，部分复用有效缓存，不把缓存命中计作新模型调用。
- 实际浏览器编辑区域/效果、提交、返回/关闭、整页刷新及两tab恢复：A/B各恢复正确session与receipt，预览实际播放，JS错误0、关闭未自动取消。A其中一次命中已完成缓存，B明确queued；不将两者都称为首次排队。
- 原项目→新项目切换、同项目多素材/两tab实际覆盖；跨三项目晚回执的所有排列未逐一真实重复，另有同版本前端隔离回归证据。
- 字幕曾真实503：启动参数误指不含GEMINI_API_KEY的开发env，trace`511ef0d317f64a38960a768de7e0feb8`。只修既有凭据路由，选择读取已获授权且风险识别实测的Gemini文件；随后两验收账号真实入队、执行、completed，分别98/99条cue，每条均有原文及中文，重复请求同报告。核对cue时序/时长范围、无error、识别预览SHA/字节/真实音视频及当前master绑定SHA；该功能输出cue/UI覆层，不生成另一份烧字幕导出视频。字幕不是只看202。
- Gemini字幕阶段原OpenAI桶SHA不变。AI1最小普通输出记录`ai_semantic/cache_reused=false`，真实`hook_emphasis` HTTP200、实际配置模型`gpt-5.6-sol`及原预算ResourceDeferred留证；没有移除其他OpenAI预算。
- 该AI1记录仍提示未调用AI画面检查：`hook_presentation.placement`既有逻辑在非production强制safe_default。**生产专属自动避让/画面检查未覆盖**，不切production、不改分支或提示规避。
- 助手入口在AI1实际DOM仍隐藏，共享解析后端保留；文本/Excel预算解除及字幕2+30/33rd拒绝/FIFO等精确边界，复用下表明确标注的隔离替身证据，没有32次真实付费请求。

## 最终输出：B PASS，A FAIL

B任务`9e2eb593cde7426ea32b8ada3aeb7f71`业务completed，文件存在、SHA匹配、6399帧、1080×1920、有音频、Range206。浏览器关闭及服务配置重启后重新打开正确最终结果并实际播放，未确认替换。

A任务`2a567ce0a14642728cbc4458640b256c`业务failed，stage composing，错误“视频处理超时，请重试；原成片保留”，HTTP诊断trace`e6975cbce0424c0e980023cb61f77916`。原6187帧、1080×1920/30fps；仅60–90帧有效遮罩，删120–150帧，预期最终6157帧，尚无通过业务校验的完整输出。**A仍FAIL，完整交付未完成。**

两最终任务串行，final_media观测峰1；A编码进程窗口601.15秒，随后B编码709.83秒。队列`done`只表示调度已处理，不能当业务成功。最终期间普通render峰0，shared media峰2来自B预览；没有普通槽位挤占证据。

只读归因：切删分支沿既有`cutting.run`600秒、render配置medium/CRF18；不删分支显式900秒、fast/CRF18。全段gblur sigma48/steps2、4编码线程/filter_complex1在此前baseline已存在；本轮没有这些参数变化。未跑完整旧baseline同片对照，不能声称排除了所有时延回归。原超时路径未保存完整stderr/随机临时目录，命令按源码和实际配置重构，不冒充原进程完整记录。

获准的隔离诊断仅取同源20秒及实际保留mask，不替代长片验收：相同600输入帧、删30帧后570输出，同规格/效果/编码参数，基线全段模糊67.4327秒；只在有效mask帧启用gblur的候选**命令**18.4084秒，570帧解码MD5全部一致。沿既有PCM裁音/AAC路径，19秒音轨解码MD5也一致，拼接样点176400→220500。没有将候选写入应用、增加timeout、降低效果或换素材。

拟议最小修复仅供审批：从每效果组的源帧区间生成blur/soft_blur的gblur timeline enable；保持原mask、强度、帧率、选择/裁音、编码和超时。需防多区域区间、删除前后源帧/输出帧混淆、分数帧率与FFmpeg版本支持；短前缀一致不证明所有组合。**父明确归为范围外待用户审批，未实施。**

## 逐变更覆盖矩阵

|变更/验收项|环境、证据与断言|结论|
|---|---|---|
|原400/四引用错误|实际18883/source05，原七集与索引，原GET errors=[]、列表200、原422文件SHA不变|PASS|
|去普通10门槛|实际18883，A/B四项目16真实render描述符，重复token复用，16业务输出检查|PASS|
|全局ordinary3/多项目公平|实际18883，5项目20输出、render峰3，各项目持续到完成|PASS（本用例）|
|满槽仍接收/独立交互|实际18883，三素材202、preview峰2、render3/media5同时116采样，真实SAM结果|PASS|
|前端刷新/重开/两tab|实际Chromium，本地保存、正确receipt及播放；更广晚回执排列另有隔离UI组|PASS（所列覆盖）|
|Gemini预算独立|实际风险识别/双账号字幕，原OpenAI桶SHA不变；其他OpenAI真实延后仍生效|PASS|
|文本/Excel预算解除|原独立main HTTP/provider transport doubles的2文本+2Excel及当前2073回归；未在18883另重复真实解析模型|PASS（隔离/替身）|
|字幕2active+30pending|实际2账号真字幕98/99cue；2+30/33rd拒绝、重复/FIFO/恢复为原独立dev提供方替身边界|PASS（分清环境）|
|同key/异key及子进程租约|实际同源跨账号/异素材预览与释放；取消/父死亡/异常释放为当前回归/隔离dev|PASS（分清环境）|
|Hook源/索引变化|96c1aa5实际worker0 dev及当前回归，相关模块对05未改；不称18883做了缺片/换片故障注入|PASS（隔离）|
|普通worker0/1、维护|当前回归覆盖main四组合；18883 worker1真实21输出，维护仅在idle，未在用户活动时强制重启|PASS|
|排队项目暂停恢复|实际A3 pause/resume、其他项目继续、全部完成；运行中强杀仅隔离|PASS（所列覆盖）|
|B最终/后台持续/重开|实际B completed+文件媒体+实际播放；未替换|PASS|
|A删帧＋打码完整最终|实际6187帧输入、同原范围/框/blur，600秒超时|FAIL，待审批|
|Hook AI语义|实际AI1单条qc_passed、ai_semantic、非缓存HTTP200|PASS|
|Hook自动画面检查|既有production-only策略，development安全默认及原warning保留|BLOCKED/未覆盖|
|助手隐藏/配置|实际AI1 DOM隐藏、候选业务资源一致，MPS保留|PASS|
|FBBM上传/发布|禁止真实外传；upload/poll仅独立替身测试|NOT RUN（按要求禁止）|
|Linux/长mask最大容量|本机代表性功能证据，不是生产容量认证|NOT RUN，不作承诺|

原独立证据及278/20历史失败映射保留于`frontend-background-resource-scheduling-2026-11-02.*`、`concurrency-scope-check-2026-11-02.md`。旧abd089f真实模型证据不重标为05；来源变化修正的96c1aa5重验另列。此次2073固定回归独立保存，不以旧2057冒充。

## 回归、配置维护与最终安全状态

- 原05整轮：2072 passed / 1 skipped / 1 failed / 10既有fork警告，955.21秒；`/tmp/d1-18883-full-05b2faf.log`完整保留。
- 失败夹具在confirm已经paint后才把mock tracking从false改true，依赖后续loadFrame重绘解锁。受控脚本等待实际帧已paint后稳定复现原3秒disabled超时；正常UI重绘后原三个业务断言通过。没有产品修复绕行。
- bbfa4f7将能力设置提前到打开UI前，原三断言不动；12次定点通过、原相关UI组94通过。
- 新固定整轮：**2073 passed / 1 skipped / 10既有fork警告，693.90秒**，`/tmp/d1-full-bbfa4f7-source05b2faf.log`。
- 用户授权的两次idle维护均备份launcher/runtime/用户保护清单/私有账号DB，持mutation与计算屏障后二次确认空闲、正常SIGTERM；回退不恢复DB。运行时JSON字节不变。AI补验只追加开发OpenAI KEY/BASE_URL两项，旧env字节前缀不动，0600；字幕只选择读取既有GEMINI_API_KEY，不整体导入其他配置。
- 收尾只读：21 render+6 fetch全部completed，interactive/subtitle pending及active均0，执行槽无占用、服务子进程0（dispatcher主租约应保留）。422原项目文件SHA相同，源SHA相同。FBBM upload/poll job0、publication文件0、真实上传/发布调用0；未点击或调用真实上传入口，未改生产。

私有完整证据根为原trial下`actual-e2e-20261103-123501/`，含`final-summary.json`、`ordinary-20-qc.json`、`multi-project.json`、`resource-samples-ai0.jsonl`、`resource-samples.jsonl`、`real-scan-reports-current.json`、`real-sam-previews.json`、`two-account-models.json`、`subtitles-verified.json`、`browser-mosaic.json`、`finals-verified.json`、`B-final-browser.json`、`hook-ai-record.json`、`hook-ai-provider-events.json`及`compose-command-diagnosis/`。账号、密钥、视频、截图和原始业务配置不入Git。失败的私有harness尝试另留日志，不冒充应用成功或隐藏为绿。
