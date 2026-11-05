# 剪切打码 OSS 原片播放方案

> 历史归档（2026-11-04）。正文保留当时的方案或执行状态，不代表当前版本；日常操作见[文档入口](../../README.md)。

状态：与 Grok 4.7 两轮审查达成一致；已实现，默认关闭、未发布。实现及 Gemini 代码审查见 [验证记录](../verification/editor-oss-playback-2026-10-28.md)。用户要求与 Grok 就安全修改方案达成一致。本任务不恢复已暂停的测速功能。

## 现有事实

- mosaic_session.open_session 用源 SHA 校验本地会话副本；metadata 来自该副本。mosaic.js.open 将 mosaicSource.src 设为 /mosaics/{sid}/source。
- cutting_http/mosaic_http 的 source 与 frame 都执行会话/项目鉴权及 require_editable；原片版本改变会拒绝旧会话。
- preview_oss 的私有对象按项目/素材/SHA 寻址；original_oss 保存 key、sha256、bytes、identity、bucket、endpoint，sign_url 的现有有效期为 900 秒。
- material_workers._receipt/freeze 在封存前校验内容和回执，复制后重绑回执 identity。会话副本与封存原片 inode 不同，不能用会话副本 identity 去验证原片回执。
- cutting_replace.prepare 已有新原片/轻量预览上传机制，不需要在播放器内重复上传。
- signed_preview 可能触发 _ensure_uploaded 的冷上传与远端 HEAD/ACL，不能直接用于播放入口的只签名快速路径。

## 已确定范围

用户最终选择先用正式成片。本轮不复用现有24fps普通预览、不生成编辑代理、不改FPS/码率/分辨率。只调整下载链路，不改变被播放的字节内容。

## 媒体路径清单

| 页面/用途 | 当前文件与路由 | 内容身份 | 本轮处理 |
|---|---|---|---|
| 工作台成片预览 | preview-url → 当前轻量预览 OSS（缺预览时可能回原片） | 当前版本的preview.oss，普通预览固定24fps | 不改 |
| 剪切打码原片 mosaicSource | 会话 source.mp4，mosaics/{sid}/source | 打开会话时SHA绑定的正式成片副本 | 同SHA original_oss优先，本地兜底 |
| 结果对比“处理前” mosaicBatchVideo | 同一会话 /source | 与mosaicSource同一个旧master | 复用同一源管理与SHA，不重新取当前素材通用下载 |
| 结果对比“处理后” mosaicBatchVideo | /render/{jid}/video → render-{jid}/mosaic.mp4 | 尚未替换的完整处理结果；删除片段后时间轴可能缩短 | 保持任务专属本地路径，不套用旧master签名 |
| 局部效果预览 mosaicTrackVideo | /render/{previewJob}/video → render-{jid}/mosaic.mp4，job.preview=true | 选定范围的缩小预览；process_preview裁剪、重置PTS、保持源fps | 保持独立任务路径，不按完整原片时间轴恢复，不允许替换完整成片 |
| 动态跟踪预览 mosaicTrackVideo | /track/{jid}/preview → track-{jid}/preview.mp4 | 跟踪任务产物 | 保持独立路径与生命周期 |
| 旧剪切原片 cutSource | cuts/{sid}/source → source.mp4 | 会话绑定正式成片 | 与mosaic共用安全源解析/管理逻辑 |
| 旧剪切结果 cutOutput | cuts/{sid}/output/{jid} → {jid}.mp4 | ready/saved剪切任务结果 | 保持任务专属本地路径 |
| 编辑器结果下载 | cutDownload与mosaicBatchDownload指向对应任务结果 | 应下载处理后的字节，不能下载当前旧master | 不改 |
| 准确帧/画框 | /frame/{index} → 会话frames/*.jpg | 从该会话master抽取 | 不改 |
| 确认替换后工作台/重新打开编辑器 | cutting_replace.prepare/commit生成新master+preview及OSS回执，切换当前版本 | 新SHA，旧session不得续签 | 重新建立/获取新SHA会话，不把旧会话指向新源 |

结果对比前后可能有不同duration和时间轴，不能把source恢复位置机制无差别应用到after。两个控件共用“处理前”源时仍保留各自播放意图/时间/加载代次；切到after后，迟到的before续签不得覆盖after。paintBatch每次重绘不得重新签名或重置src。

### 已核对位置

mosaic.js open/paintBatch/mosaicPreviewClip/mosaicTrack，cutting.js showJob/open，mosaic_render.video_file/process_preview，mosaic_tracking.preview_file，cutting.output_file，mosaic_replace.start/run，cutting_replace.prepare/commit。

## 后端

1. 为 cuts/mosaics 新增会话级 source-url GET（新增接口契约，须由实现与测试共同定义，不推测已有 API 字段）。沿用现有登录、项目权限、批次租约及 require_editable 的版本检查。
2. 获取当前 master 和 record；session.source_sha256、record.output_sha256、original_oss.sha256 必须相等。检查回执 key 恰为当前命名空间/项目/编码后的素材ID/SHA.mp4，bytes 与 master 大小一致，identity 与当前 master 一致，bucket/endpoint 与实际集中配置连接一致。
3. 校验失败不签发任何 OSS 地址；回执缺失/不匹配、未启用 OSS 或签名配置失败可选择本地原片，并记录有限枚举原因。访问无权、会话失效、当前版本变化等业务拒绝不能伪装成可回退的 OSS 故障。
4. 只对已验证回执签名，不调用上传、逐次全文件哈希或逐次远端 HEAD。签名前后重新确认版本；不在普通页面轮询中签名；响应 no-store，签名URL不落持久状态、不输出诊断日志。
5. 本地 fallback URL 仍经过原有权限与版本检查。不能用当前素材通用下载地址替代会话绑定，因为当前版本可能改变。

## 前端

- 独立小型源管理模块供两个编辑器复用，保持现有 playAttempt、epoch/context 防护。解析源后只加载一次；短超时可转本地，超时后迟到的OSS结果不可自动覆盖本地。
- 源管理记录会话、请求代次、媒体加载代次、目标时间、播放意图与恢复次数。关闭、重开、切素材、显示结果、暂停、拖动都必须使不再适用的恢复失效。
- 切换源时保留目标时间、音量/静音、播放速率，等 metadata 后定位，确认有效会话和最新播放意图后才恢复播放。不让加载引发的 pause/timeupdate 事件改坏编辑选区或 wanted。
- 不定时替换正在正常播放的 src。签名临近过期时，仅在新的播放/定位动作确有需要时获取新地址；续签前校验会话。处理异步后丢失浏览器播放手势的情况，NotAllowedError 提示用户再点击，不循环重试。
- 恢复有界：自动媒体错误恢复至多重新签名一次、再回退本地一次；本地也失败则停止并给出重试入口。预算只在用户显式重试或重开会话时重置，不能每个 waiting、seek 或短暂 playing 都重置。暂停/拖动使旧恢复任务失效但不补充预算。主动取消的 AbortError 不触发换源，当前有效异常仍显示，不能按异常名全吞。
- waiting/stalled 不代表OSS故障，尤其正在seek或后台页。前台有播放意图且连续10秒无currentTime进展时展示手动切换服务器入口，不因普通缓冲反复换源；seek卡住用单独的定位超时提示提供同方向入口，不自动重签。本轮只提供OSS到本地的手动降级，不做本地到云端的双向切换。播放和准确帧提示独立，不能显示“正在播放”掩盖等待。

## 安全与回滚边界

签名是有时限的 bearer URL：发出后即使账号退出或素材替换，也可能在剩余有效期内读取旧对象。这与现有成片直连一致；续签与编辑提交继续做鉴权和版本校验，不能声称即时撤销。旧链接绝不会指向新版本内容。

在既有集中 cutting 配置中加默认关闭开关。关闭后新打开会话走原有本地源；已经打开并持有OSS URL的页面需重开编辑器/刷新才完全回退，不能声称开关即时撤回所有现有播放。

## 验收矩阵

- 后端：无权限、跨项目session、版本变更、回执SHA/key/identity/bytes/bucket/endpoint不匹配、缺回执、OSS关闭、签名异常；不发生上传/远端HEAD；已有本地接口照常受权限保护。
- 前端：初次OSS成功/超时、本地回退、源解析迟到、签名过期、恢复过程中暂停/拖动/关闭/重开/切结果；旧事件不改变新会话；播放位置与暂停意图保持；本地失败终止；NotAllowed/主动Abort不误触发恢复。
- 内容：整数和分数帧率、开头/中间/结尾，多位置暂停准确帧、删除与打码边界保持；OSS Range返回的样本字节与同SHA本地原片对照。已有长GOP准确帧测试继续通过。
- 隔离环境模拟慢网络、断网、OSS403/404、等待无error、快速拖动；多用户制作时验证可播放，但不声称此方案消除所有CPU/磁盘/网络瓶颈。
- 测试实例真实浏览器验收后再灰度；发布前全量回归及既有发布管理器门禁。观察首帧/等待时长、源选择与回退次数，不采集签名URL。

## 顾问审查

通过 Paseo Advisor 调用 Grok 4.7，agent 1dd47286-82cb-4c6d-a67d-3031d01682a7。首轮明确同意正式成片方案，并独立核对媒体路径。

已采纳：before与主视频共享会话签名；身份比对current master而非副本；签名快速路径不调用signed_preview；版本/权限失效不得降级；有界重签/本地恢复；结果与预览路径保持；不改CORS/ACL。

第二轮收敛：去除本地到云端手动切换，减少状态组合；NotAllowed只提示用户点击，不必移除既有播放错误状态机制；明确预算只显式重试/重开重置；source和before共享源身份但各自独立加载代次。顾问第二轮明确回复“同意这七条，作为最终方案”。

### 第二轮确认的实现边界

- 恢复周期从打开后的第一次赋源开始，覆盖video.load失败；预算仅显式重试/重开重置。
- 手动“切换服务器”仅当前源为OSS时显示，不被重签预算耗尽挡住；落到本地后不得自动签回OSS。一边恢复不能替另一个video调用play。
- 切源前增加playAttempt，使旧play Promise的主动取消进入既有过期过滤；当前有效Abort仍提示失败。NotAllowed仅提示点击播放，不消耗换源预算。
- 处理前赋源前再确认resultBefore及加载代次；旧响应不可覆盖处理后。共享的是会话源解析结果，非两个video的播放状态。

结论：所有播放路径已逐项区分，本轮只把会话正式原片及处理前对比迁到OSS。产品实现已完成，发布尚未执行。

### 实现阶段的原生控件续签细化

Gemini 代码审查后，处理前原生播放器的 play 事件只预取续签，不立即换 src；正常播放不被打断，新地址在下一次 seek 或媒体错误恢复时采用。主编辑器自定义播放按钮仍可先续签后播放。原生 seek 接续在途解析时保留最新目标和意图，同时使旧恢复代次失效；不增加恢复预算。
