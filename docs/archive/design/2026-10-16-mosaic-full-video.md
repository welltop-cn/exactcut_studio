# 多区域完整视频生成（本地首版）

> 历史归档（2026-11-04）。正文保留当时的方案或执行状态，不代表当前版本；日常操作见[文档入口](../../README.md)。

入口：采用片段后，底部“生成完整打码视频”一次提交所有已采用区域。输入冻结为当前保存的框和时间范围；未采用区域不参与。当前沿用最多 8 个区域、每区域最长 10 秒/300 帧限制。完整源视频最长 5 分钟，输出保留全部帧、原分辨率/帧率和原音频，不覆盖原视频，也不触发剪切替换或上传。

## 应用契约

- `POST /api/batches/{bid}/mosaics/{sid}/render`：`{reset_at,targets:[{start,end,seed_frame,x,y,w,h}]}`。帧范围为 [start,end)，像素为原片坐标；校验同单区域跟踪，去重并排序。返回 202 + job。
- `GET .../render`：`{job:null|job}`，恢复最近一次生成。
- `GET .../render/{id}`：当前任务。
- `GET .../render/{id}/video`：认证和当前源版本校验后的完整 MP4 视频流，前端同源 download 链接下载独立新文件。
- job：schema_version=1、id、status（running/completed/failed）、stage（tracking/composing/completed）、source_sha256、reset_at、targets、created_at、progress、completed_regions、total_regions。
- 完成 result：metadata、sha256、bytes、skipped_frames（去重后有区域跳过的帧数，不代表其他区域也跳过）、region_count、warnings（region、frame、reason）、notice；completed_at 作为视频 URL 缓存版本。失败仅返回安全错误，不向浏览器暴露内部日志。

## 后台流水线

沿用 compute 和 batch 共享租约，保证同一时刻只有一个重型生成任务；每个 render job 有 worker/state 锁。任务身份包含源 SHA、reset、完整 targets、模型配置、合成引擎和降级策略。相同输入重用运行中/已完成任务，失败任务可重试，源或采用范围变化创建新任务。最近一次 job 指针支持关闭重开恢复；编辑后的采用范围与现有成品不一致时，界面提示重新生成。

按区域串行执行 SAM 2，优先复用同身份单区域跟踪结果或该批次已完成的区域 mask；不得从共享计算租约内递归调用单区域入队接口。每个原片帧合并所有有效区域 mask。按用户要求，空遮罩跳过该区域；模型任务失败，则跳过该区域整个作用时间。其他有效区域继续合并，不整帧打码。诊断保留在后台，界面不展示失跟告警。缺失/损坏的最终 mask 文件不能静默当作无区域。

合并 mask 以灰度 rawvideo 流写为 FFV1 中间视频，避免将全片几千张原分辨率帧写入磁盘。最终 FFmpeg 将原片分为原图、像素化两路，以合并 mask 做 maskedmerge，转回 yuv420p/H264；音频直接 stream copy。mask 在 RGB planar 空间合成，避免有限范围 luma 白色被解释成半透明遮挡。Matroska 时间戳会四舍五入，因此进入帧同步前用原始有理帧率 settb + setpts=N 恢复准确时间轴。

完成前重新验证输出全部帧时间、尺寸、帧率、音频存在情况与原片 metadata 一致，再原子发布输出，并复查源 SHA/reset。原片始终只读。异常/中断不会发布半成品；重新发起恢复已完成区域推理。

## 验证边界

自动测试使用真实 FFmpeg、合成视频和人工 mask，覆盖重叠区域、空遮罩、缺失 mask、完整帧序、30000/1001 帧率、音频字节保留、原片不变、认证/CSRF、重新进入和下载。

SAM 2 对局部目标仍存在已知传播波动；该版本只提供完整生成链路与跳过失跟区域策略。自动警告不能覆盖所有漂移，无警告也不是“审核通过”。未实现人工关键帧纠偏或自动切镜断轨。

## 2026-10-16 交互更新

“预览本片段效果”固定显示，选择片段且已有完整成片后可用。直接从最近一次完整成片定位播放所选时间段，包含生成时所有区域，不重新跟踪；当前取消采用不影响查看旧结果。到片段结束暂停并回到起点。单区域模型预览保留，二者明确区分。

取消整帧兜底后，批量缓存键策略改为 skip_region，单区域预览引擎版本升级到 2，避免重新生成时复用旧整帧打码成片。已有成片保持不变。
