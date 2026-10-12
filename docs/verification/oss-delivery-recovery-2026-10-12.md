# OSS 交付校验与重试修复（2026-10-12）

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

发布与线上结果待完成后补充。
