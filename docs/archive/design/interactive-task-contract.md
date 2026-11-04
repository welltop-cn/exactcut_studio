# 本地交互任务协议（候选版本，schema_version=1）

> 历史归档（2026-11-04）。正文保留当时的方案或执行状态，不代表当前版本；日常操作见[文档入口](../../README.md)。

仅为本次接收/执行分离补充协议。唯一业务规则见父仓库`docs/archive/design/frontend-background-task-policy.md`。普通jobs、字幕、最终mosaic的既有队列仍各自权威，不转换成新数据模型。

## HTTP

|入口|已就绪/缓存命中|需要处理|
|---|---|---|
|POST `/api/batches/{bid}/mosaics/open`、`cuts/open`|200，既有完整session对象|202，原片准备job；GET `/{family}/preparation/{id}`查询，completed的`result.session`是完整session|
|POST `/mosaics/{sid}/scan`、`locate`、`track`、`clip-preview`，旧cuts scan|复用已有completed或活跃job|202，既有job字段加queued状态；沿原GET入口查询，不因执行池满拒绝|
|POST `/api/batches/{bid}/hook-preview`|200，completed job（`result`含原Hook响应）|202，queued/running job；GET `/hook-previews/{id}`查结果，GET `/hook-previews`恢复列表|
|GET `/{family}/{sid}/frame/{index}`|200 image/jpeg，不占短媒体执行槽|202 JSON job；重复同GET只查询/复用同任务，后台生成后同URL返回JPEG。初次调用可带`X-Exactcut-Frame-Retry: 1`重试先前失败，后续轮询不带该头；失败明确400，不无限自动重试|

所有路径前缀保持原有base_path逻辑。权限、CSRF、输入/来源/版本/maintenance/最终冻结照常校验。cuts准备必须在接收前及短入队事务内验证可编辑，不能用冷202绕过最终冻结；mosaics准备保留不可变源的只读打开能力。任务ID、路径和runner由服务端构造，客户端不能提交任意文件路径或函数名。

## 报告状态与结果

- `queued`：已持久接收，等待后台；不是尚未接收，也不是每个请求创建了一个等待线程。
- `running`：受控worker开始推进；可在转换阶段时等待资源，worker数有界。
- `cancelling`：已有取消意图，仍等待实际进程退出；不提前释放租约。
- `completed` / `failed` / `cancelled` / `superseded`：成功、真实失败、取消、输入或用途已过期，互相区分。

已有job中的id、actor、source_sha256、reset_at、progress、created_at等保留。Hook报告额外记录request_id、index、revision、configuration_sha256、source_identity_sha256；完整GET返回input_current，核对当前同素材输入、配置以及原片/shot-index文件身份。旧缺source_identity_sha256报告仍可读，但不标记当前可采用。原片从缺失到位、替换或索引变化会产生新回执，无需修改草稿；后台原有内容hash和缓存校验继续执行。Hook结果不自动覆盖新编辑。不同素材的Hook不会仅因另一行preview_key回执变化而失效。

报告是业务结果权威。新`.web-state/interactive-queue/queue.json`只保存pending/active、顺序、owner游标和经过服务端验证的执行描述符；包含必要输入快照与内部只读permit，不含API密钥。描述符先落盘，报告发布被打断时可从该本地outbox恢复。旧报告继续读取，新增可选字段不改变schema_version。

## 执行和公平

HTTP只登记。dispatcher探测相应worker和首阶段执行资源，拿不到就保留pending，不开启一条阻塞线程。交互dispatcher与最终/字幕一样独立于普通制作的EXACTCUT_WORKER_ENABLED开关；开关0时普通制作仍禁用，已接收的交互可执行。只读viewer必须显式拒绝写/提交，不能靠接收后永不执行实现只读。scan、preview（含Hook）、preprocess、frame按组及owner轮转。没有每账号单槽；同账号可有两个预览worker和多个等待素材。

- 普通阶段沿生产基线fetch2/render3/upload2/poll4，无额外总1；普通制作3是全实例额度，不是每项目3。
- 最终mosaic业务1、字幕业务2+30独立保留。
- 交互scan2、preview2、preprocess2；短媒体2。
- 初版媒体profile为共享长媒体5+最终专用1（总6），使普通3与两个用户预览具备执行机会，最终不会被共享池持续挤占；这是隔离验证起点，不是Linux长片容量结论。
- OSS传输2；按endpoint/bucket/不可变对象key跨进程去重，不再使用64散列锁碰撞串联异对象。
- Gemini及文本/Excel不扣原应用RPM桶；其他OpenAI原预算保留，Hook因该预算需等待时回到持久队列，而非HTTP容量429。

SAM父进程拿到媒体租约后，在仅服务端写入的request.json中传递`execution_lease`（fd/device/inode）能力。子进程核对真实继承FD及媒体锁文件身份，再复用该阶段；嵌套FFmpeg不重复取槽。FD关闭或被复用到无关inode时拒绝继承。worker/cache/batch/stage FD仍由guard和子进程实际退出决定生命周期。

## 缓存与过期

同key的业务身份、生产者锁和原子发布保留。队列中的同素材同用途旧预览可以supersede；不会清掉其他素材任务。准确帧对同用户同会话尚未执行的旧取帧合并为最近请求；不同用户互不替代。缓存命中直接返回，不排在长片制作后。

已在执行的云端请求不能承诺真正取消；本地只拒收其过期结果。页面关闭/离开不会自动取消服务器任务；重新进入查询既有身份。预览提供明确取消按钮，返回编辑与取消分开。

## 恢复和回退

dispatcher独占lease防止双调度。重启后等待任务保留；安全本地preview/frame/preparation在确认没有活worker/继承子进程并重新验证输入后可重排。已执行但结果未知的识别/Hook云请求标记中断，不自动承诺恰好一次。最终与字幕继续用自己的恢复逻辑。

旧版本不理解新交互等待协议，回退必须先自然排空或显式取消新交互pending/active，并确认owned子进程退出；保留任务报告、数据和用户数据库，不能靠恢复旧DB回退。18883在父review和无活动用户任务前不更新。
