# 飞书临时读取失败自动恢复

## 事故证据

生产任务 2b49b3f61ee64c88bcaa889110bfdd12 的第二条交付，在已有飞书记录、ADS 素材 ID 和视频 ID 后，读取飞书记录返回 code=1255001、msg=InternalError。原流程把所有轮询异常都归为 unverified，停止查询。通过既有管理员 verify-delivery 入口只读核实后，两条原素材均为 ready；未重新上传或导入。

## 修改边界

- 仅已知 GET 读操作的临时失败使用 TransientDeliveryReadError。飞书 get_record 对实测 1255001、HTTP 429/5xx、连接错误和超时最多请求三次，间隔 1、2 秒；其他错误立即交由原处理。
- FBBM 已知 GET 状态/额度接口的连接错误、超时和 HTTP 429/5xx 可以延期查询。登录、导入、重同步、飞书写入失败不借此重放。
- 轮询遇到上述异常，只有原飞书记录、素材 ID、批准 SHA、OSS URL/key 均唯一且完整，才保留原 intent 为审计历史，保存 verification_only，保持 fbbm_queued。后续调度只能读取原记录和素材，不能 ingest/resync/写表。
- 继续使用集中配置的 poll_seconds 和原 poll_started/wait_timeout_seconds，不重置截止时间。到期不再启动新轮询；已开始的有限 GET 尝试允许结束。这不是严格的请求级硬截止。
- 暂时失败显示自动查询提示。恢复为 syncing/ready 时清除旧提示。永久失败、身份冲突、回执缺失、超时保留待核实屏障。
- 不自动清除历史已锁定的未知交付，也不改变 OSS 复制、渲染加速或当前未提交的静态片尾工作。

## 验证

14 项新增回归覆盖短重试、持续故障、未知错误、HTTP/网络异常、读写分类、自动切换只读恢复、重复失败保留期限、身份缺失/冲突、到期停止、恢复清提示。连同 round-one delivery 共 56 项通过。独立审查无阻断项。

完整回归和发布验收另行记录；此文件不表示已上线。
