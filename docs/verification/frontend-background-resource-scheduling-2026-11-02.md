# 前台接收、后台受控执行：固定候选验收（2026-11-02）

基础源码候选：`abd089fda3f2b60ed66e8d2359348d2f6860bd8b`，基线`9b4b2bb`，隔离分支`feat/frontend-background-resource-scheduling`。父review后最小修复为`81d66ac`和`96c1aa555e35c394178723afd7d65e22c0142597`，当前固定源码为后者。下文真实模型混合和provider边界证据仍属于abd089f；不冒称已在补丁版重复模型验收。补丁及新源码重验单独记录。父最终review随后通过，2026-11-03仅本地18883获授权并完成空闲维护升级；见[升级记录](frontend-background-user-upgrade-2026-11-03.md)。以下验收过程保留各自版本与当时状态。

机器可读证据：[JSON](frontend-background-resource-scheduling-2026-11-02.json)。失败和原断言映射：[范围审查](concurrency-scope-check-2026-11-02.md)。协议及回退：[任务契约](../design/interactive-task-contract.md)。业务依据仍是既有统一方案，不采用调查附件中的其他额度方案。

## 固定源码与完整回归

|提交|审查关注点|
|---|---|
|`9860f90`|Gemini和文本/Excel解释退出原应用共享RPM；解析网络在短发布事务外；离线调用保留原事务归属；仅隐藏助手入口|
|`fa42015`|真实identity锁、媒体阶段租约、SAM继承FD和实际子进程所有权；不引入账号接收额度|
|`abd089f`|持久交互接收和原worker适配、前端queued恢复、报告/生命周期正确性；普通去10、字幕30和候选有效配置|

固定候选完整命令（工作目录`source`）：

```bash
/Users/matao/miniconda3/envs/py3.12/bin/python3.12 -m pytest -o addopts='' -q -o faulthandler_timeout=45
```

基础abd089f结果：**2049 passed、1 skipped、10条既有multiprocessing fork警告，660.13秒**。日志`/tmp/d1-resource-scheduling-full-abd089f.log`。首轮中断不是全量通过：它暴露离线恢复已持非重入mutation锁而Excel内部再获取的问题；HTTP显式传短发布锁，离线默认由调用方保持事务。恢复/导入相关42项通过后才固定本候选重跑全量。

abd089f基础源码相对基线：生产33文件、1057增/427删；测试48文件、1245增/300删。五个新生产模块共445行。新增范围冻结后只修复已复现的stop/claim、维护屏障、outbox、报告锁、晚回执和cuts准备绕最终冻结等问题。原278通过/20失败逐项原断言→替代业务断言、真实回归与测试桩错误的分类保留在范围审查文档，未用删额度/Semaphore断言代替业务验收。

## 父review补丁与新源码重验

- `81d66ac`：普通制作`worker_enabled=0`不再关闭独立交互dispatcher。原来实际main冷cuts/mosaics两例queued永不推进，worker1对照通过（红2失败/2通过）；修后主入口/维护/关闭/启动失败14项通过。普通worker开关和外写开关均未改成1，交互启动仍在原try/finally内。
- `96c1aa5`：Hook完成回执绑定源和shot-index的文件身份；三例真实submit/compute红灯为缺原片完成后补片、原片替换、既有索引变化。HTTP只stat元数据并计算身份摘要，原compute的内容hash/缓存校验不变；执行前及发布前复查源。另验证计算中源到位时旧占位结果superseded且不发布。48项相关回归通过。旧无源绑定报告可读，但不标记input_current。
- 父独立审查上述最小diff并复跑实际main、Hook源身份、队列生命周期和Hook HTTP，14 passed/12.79s。
- 新源码`96c1aa5`的实际development主入口，使用同一候选有效配置、普通worker0、发布/外写仍禁用：cuts与mosaics冷准备均202→completed、冷帧均202→200 JPEG；维护时pending不领取；普通`/api/jobs`保持503拒绝。真实Hook先缺原片完成，再补原片、同草稿同配置再提交得到新ID/真实帧；旧input_current=false；源不变再提交复用ID。无provider替身，新增付费模型请求0；服务退出码0。证据`review-worker0-dev.json`、`review-worker0-http.json`。
- 新固定源码`96c1aa5`完整回归：**2057 passed、1 skipped、10条既有fork警告，673.17s**，日志`/tmp/d1-resource-full-96c1aa5.log`。相对基础版新增4个实际main组合、4个Hook源身份/发布保护用例；全量后源码无改动。

## 候选有效配置和环境差异

本次比较对象是**同版本候选生产基线**，不是线上runtime实读。实际服务由`exactcut.webserver.main`启动，并在真实Server构造时执行有效配置相等检查；没有只调用`worker_tick`代替本地dev。

|项|候选和实际dev有效值|
|---|---|
|普通lane|fetch2 / render3 / upload2 / poll4；推导总11|
|最终mosaic|1|
|字幕|2运行、30等待|
|预览 / 识别 / 预处理|各2执行；不限制账号接收数|
|长媒体|共享5、最终专用1|
|短准确帧 / transfer|各2|
|普通FFmpeg / 交互encoder / SAM CPU线程|4 / 2 / 4|
|保留的其他OpenAI预算|6 RPM、burst1；Gemini和文本/Excel不消费该桶|

整个concurrency段缺失的独立配置读取按每lane1推导总4；正常集中default和候选production合并为2/3/2/4总11。显式`max_active_children`应急总数仍优先，原total8和total1案例保留。

完整cutting处理配置另与已知模型runtime比较：排除声明的`tracking.device`和外部OSS播放开关后，字段和值全部相等，并保存规范化SHA。已知设备mps、实际dev为cpu。该已知模型配置是明确选定的候选来源，不称当前线上配置。细节在私有`model-configuration-comparison.json`和公开JSON的对应字段。

必要环境差异只有临时端口/loopback、独立持久数据根、测试账号/凭据、CPU设备、禁用外部发布上传/OSS，以及明确标注的provider替身。真实核心阶段使用授权的既有Gemini凭据；边界阶段使用本地占位凭据和provider transport替身。未更换限流域、清空桶、修改时间或靠新桶绕开限制；后续文本/Excel测试只在同一桶正常获取一个token使其耗尽，再比较前后哈希。

**媒体5+final1目前只有Mac代表场景的功能证据，不能视为Linux长片、长时间掩码的最大承载或生产容量核定。**

## 基础候选abd089f实际dev核心矩阵

以下核心与后续provider边界均为abd089f，不是96c1aa5补丁重验。

所有行均为同一源码候选；端口由实际主入口选择，数据根为`source/.dev-state/resource-scheduling-20261102/c3ef0f13/data`。这些文件不提交Git，清单和密钥文件没有纳入公开证据。

|入口/场景|实际结果|证据与边界|
|---|---|---|
|同账号三素材clip-preview|均持久接收；预览执行最多2；三个实际SAM完成，掩码产出重叠7.599秒|先由验收进程持有两个真实preview FD强制制造queued；释放后真实CPU执行。无provider/帧结果替身|
|A最终执行期间，B粗筛、其他账号定位|4次真实Gemini调用全部成功，原共享预算文件哈希不变|包括浏览器触发的粗筛；SDK只是观测，不替换结果；未付费执行32份字幕|
|A最终+两账号交互+普通3|两个用户实际掩码产出重叠11.843秒；交集内有9个采样点同时见普通FFmpeg3、共享media5和final_media1|不是仅以线程存活推断模型重叠；保存实际mask文件mtime和实际进程树/FD槽采样|
|创建、读取、保存|44次HTTP请求p50 4.84ms、p95 37.15ms、最大86.16ms|元数据短请求；不把实际视频/模型总耗时算作短请求延迟|
|真实页面queued/离开恢复/播放|排队文字可见、返回保存仍可用、关闭重开恢复同任务、真实video播放时间>0.05秒、JS异常0|独立Chromium，没有API/JPEG/model mocks；用户Chrome未开放调试，未改其设置或标签页|
|B最终排队、C正确版本打开保存|A运行时B保持queued；C用自己source SHA打开并保存；A/B随后自然完成|最终冻结、source版本校验和当前参数保护均未放松|
|最终编码结果|A6187帧1080×1920；B60帧160×96；两者各有1路音频，尺寸/帧数和源匹配|每次SAM只选6帧区域；A完整6187帧确实编码，但不能据此宣称6187帧SAM容量|
|三条普通制作终态|实际每请求结果均`qc_passed`|通过权威`workflow.result_records`读取逐请求结果，不假定仍写legacy汇总results.json|
|源和配置|原源文件、三账号源副本SHA不变；有效配置相等|没有确认替换输入，也没有操作18883用户数据|

观测进程树RSS求和峰值6738.09 MiB；`ps`生命周期平均CPU求和峰值968.1%，它不是瞬时CPU采样。临时磁盘记录为可用空间，最小4804.42 GiB，不宣称I/O吞吐。实际角色峰值preview2、scan2、frame2、shared_media5、final_media1；transfer阶段短于采样间隔，没有用“峰值0”推断未执行，跨进程transfer和继承保证另有完整回归测试。

私有核心证据：`verified-real-dev-evidence.json`、`samples.json`、`models.json`、`network.json`、`http-calls.json`、`followup-http-calls.json`、`browser-complete.json`、`browser-queued.png`和`browser-completed.png`。

## 实际dev边界矩阵：替身逐项声明

这些场景仍启动实际`webserver.main`和原dispatcher/worker。替身只处于OpenAI response transport、字幕Gemini response和catalog远端子进程；队列、HTTP、FFmpeg代理、文件锁和缓存发布均走实际实现，不作为供应商准确率证据。

|场景|结果|
|---|---|
|2文本+2Excel同时解释|四路均进入provider transport；HTTP为200/200/201/201；同桶耗尽仍不被原RPM阻挡，前后哈希一致；provider等待时新建耗时48.26ms|
|字幕2+30|先等两个实际worker进入provider，再接收30个pending；第33份明确429未接收；重复提交不增加pending；pending顺序与提交一致；32份最终completed|
|Hook六任务|预览执行槽忙时六个均接收；重复提交返回同ID；actor只取认证会话；修改其中一个输入后旧结果superseded，其余五份实际渲染完成|
|维护+进程重启恢复|六个Hook加一个冷准确帧共7个pending，维护时无active；实际停止并重启main后仍7个pending；相同帧回执ID保持；撤维护/释放真实槽位后完成|
|冷准确帧|同URL202 queued最终200 JPEG；精确帧由真实FFmpeg产生，不用JSON伪装图片成功|
|普通累计10门槛|catalog远端替身被阻塞时实际HTTP接收12个普通任务，磁盘记录均pending/running，随后12个完成；这是接收门槛证据，render3另由真实媒体场景证明|

边界证据：`boundary-evidence.json`、`boundary-http-calls.json`、`fake-provider-events.json`。这些场景没有增加真实Gemini调用，真实计数仍4。边界数据是合成视频与明确标注的准备好素材；字幕返回空cues用于队列边界，不声称模型字幕质量。

upload/poll外部写入在本地dev禁用，不作为真实供应商发布/轮询结果验收。其受控执行配额和与其他lane并行由固定候选完整回归中的实际子进程调度专项覆盖，不能把这些替身或专项写成生产发布验证。

## 保留的失败和断言

验收不是一次无错误流水。以下错误均保留，未通过放松应用断言解决：

- 核心第一次流程已取得真实Gemini/SAM/媒体混合样本，后续脚本误把A的SHA用于重新打开C，应用正确409。只清理该脚本自己的任务。`attempts/02-controller-wrong-c-sha`保留现场。
- 补验用正确C版本打开并保存、A/B自然完成。脚本随后误等待legacy `production/results.json`，实际三个逐请求结果早已QC通过。只读权威记录及编码文件核对，未修改结果、QC、source或应用。`attempts/03-legacy-result-path`保留现场。
- 边界transport夹具最初配错Excel模板，引用完整性检查正确拒绝；改用该已知response对应的标准模板。字幕替身最初缺少明确本地凭据路径，适配器正确503；补声明的占位凭据。对应attempt04/05保留。没有绕过环境或凭据保护。

其他应用正确性红绿证据仍见范围审查，包括stop有限超时不得早放owner FD、维护/stop/claim事务竞争、missing-report outbox、取消state_lock、跨tab晚回执和cold cuts最终冻结。完整回归保留原取消、恢复、版本、幂等、冻结、实际输出、源SHA和缓存不发布断言。

## 交付边界

本轮没有数据库迁移，原报告schema1和普通/final/字幕权威保留。新前端需要随新异步HTTP契约一起更新；旧客户端不能将冷open/frame的202当同步session/JPEG。回退前需排空或显式取消新pending/active工作，并等实际子进程退出，不能只删队列文件或关闭页面。

本轮不合并main、不发布生产。18883已在父review通过、实际空闲和维护/备份/回退条件满足后单独授权更新，详见上述升级记录；未取消用户任务。范围外确认问题单独列在[待审批清单](concurrency-followups-awaiting-approval.md)，当前无确认项。
