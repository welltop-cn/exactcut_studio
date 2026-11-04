# OSS 交付校验与重试修复（2026-10-12）

> 历史归档（2026-11-04）。正文保留当时的方案或执行状态，不代表当前版本；日常操作见[文档入口](../../README.md)。

## 已确认原因

任务 `2b49b3f61ee64c88bcaa889110bfdd12` 的两条交付在 16:47 北京时间失败。不可变阶段结果保留具体错误“OSS上传后校验失败”，之后 finish_job 将其覆盖成“交付阶段中断”。

生产 OSS 实测：同一对象的 get_object_meta 返回 200 和长度，但不含 x-oss-meta-sha256；head_object 返回 200、长度和与批准成片一致的 SHA 元数据。两条目标对象分别 427682356 / 487020773 字节。按目标 OSS URL 搜索飞书记录均不存在。此核验为 HEAD 元数据与本地文件哈希比对，未声称重新下载远端全部内容做哈希。

## 修复

- OSS 内部复制逻辑不变；新对象复制后及同名对象复用均用 HEAD 校验自定义 SHA 元数据。保留禁止覆盖、来源 ETag 和批准 SHA 检查。
- 发布入口仅将 OSS 准备函数抛出的异常封装为 DeliveryPreparationError。该边界尚未调用 FeishuClient，可记为 preparation_failed/publish_failed，允许用户重试。飞书/FBBM 调用结果不明仍保存 pending/unverified。
- 调度结束保留具体错误；纳管同步只在有明确 preparation_failed 且无下游回执/核实历史时解除临时待核实状态。正常进行中的 pending intent 仍阻止重复交付，但不提前显示故障。
- 项目横幅区分上传中、上传失败、待核实。交付失败不遮蔽新候选的制作、排队和失败状态，保留制作控制。待核实按钮显示原因，可重试失败沿用现有重试入口。

## 回归与恢复约束

回归先复现 GetObjectMeta 缺少自定义头、具体错误被覆盖、失败横幅错误、OSS 失败不可重试及并发同步/候选混合状态，再修复。保留模糊下游结果禁止重放测试。

事故恢复限定原两条 job，核对不可变阶段结果、原 intent、批准 SHA、当前版本、无活进程、目标 OSS HEAD 及飞书无对应 URL。apply 要求维护标记并独占 worker.lock；宿主通过发布管理器 operation_lock/write_barrier 停止旧 web 后运行一次性容器。先备份，再恢复为 preparation_failed/publish_failed，不删除 OSS 对象、不改阶段原始证据、不自动创建新的外部记录。

## 合并发布与线上验证

用户要求同时发布另一会话的渲染加速。主工作区已合入 7a24e22（短 Hook 安全拼接）、cd46255（集中线程配置，默认4）与 f41d5f3（本修复），合并提交 d544eae88b903cab28369b5615212b3bd571a5c6。

- 合并版本本地完整回归：1080 passed，173.20 秒。Linux：1073 passed、7 skipped，211.43 秒。独立审查无阻断项。
- 加速两条长样本均通过完整解码、尾部逐帧像素、完整音频和总帧数验证；详见短 Hook 验收记录。
- 发布：us-east-20261012-combined-d544eae，镜像 sha256:925251aebae5293a0445fd416072c65c6745a9710d808a346bae34eb3ffeb2d4。单独的 OSS 候选只构建未上线。
- 原两条任务在维护屏障及停止 worker 后备份并恢复为 preparation_failed/publish_failed。check-switch 确认 active_tasks=[]、phase_blockers=[]、allowed=true；保留维护标记防止旧代码接到重试，然后由 linux-ops recover --version 启用已封存合并版本。最终 healthz=ok、production、worker_alive=true、maintenance=false。
- 容器实际可导入 hook_splice；集中配置 media.encoder_threads=4。
- 公网页面显示“上传失败，请查看原因后重试上传 FBBM”；两条按钮均为“重试上传 FBBM”，勾选确认后 disabled=false。
- 沿用用户此前确认的原版本与 SHA，通过原 publish HTTP 入口逐条重试。两条均保存 oss_existed=true 和独立飞书记录，进入 fbbm_queued；未重新上传视频文件或重新渲染。验收结束时两条均已取得 material_id 与 video_id，平台状态 syncing、error 为空；尚未确认 fbbm_ready，不将平台接收等同于处理完成。
- 私有备份：/srv/exactcut/data/.web-state/acceptance/oss-preparation-20261012。原阶段结果和历史失败交付保留；发布与恢复记录位于 /srv/exactcut/history/。
