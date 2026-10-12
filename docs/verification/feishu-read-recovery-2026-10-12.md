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

## 本机 dev 验收（用户要求暂不发布）

- 2026-10-12 用户撤回本次发布要求，改为先本机 dev 测试。未执行 publish/recover 或切换生产版本。此前构建的 f34d8e3 候选仅封存，且不含后补横幅修复，不能当作最终待发布版本。
- 独立 dev 位于 http://127.0.0.1:8795，数据 `/tmp/exactcut-feishu-dev-final-20261012`；未覆盖另一会话使用的 8793。启动前清除外部服务环境变量，环境 development、外部写入/发布/AI/素材获取开关均关闭；仅使用独立合成状态数据。
- 使用真实 FeishuClient.get_record、workflow.poll、纳管交付状态合并和网页代码，替换远端 HTTP 响应为实测的 1255001/InternalError。首次三次读取失败后进入 verification_only/fbbm_queued。后续本机定时驱动继续同一只读轮询；恢复响应后自动 ready。
- 浏览器后台标签在验收时模拟可见，以便运行原页面定时刷新。错误阶段页面提示“状态查询暂时不可用”“系统将自动查询原交付记录”；顶部横幅同步显示查询延期。成功后自动隐藏错误，按钮显示“已入库”，保留原模拟视频 ID。
- 累计 9 轮、25 次模拟 GET，外部写入计数始终 0；最终 fbbm_ready、error=null；成功后停止模拟调度。此验收不声称验证真实飞书服务的稳定性，也不进行新的视频渲染验收。
- 初版完整本机回归 1094 项通过。新增横幅及暂停优先级后，针对性 102 项通过（read-retry、managed scheduler verification、material versions）；最终完整回归结果另附。
- dev 发现顶部横幅原先仍写“正在上传”，补充只读查询延期提示；暂停状态保留原暂停文案。两种状态均有先失败再通过的回归。
- 浏览器证据：`/tmp/exactcut-feishu-dev-retrying-final.png`、`/tmp/exactcut-feishu-dev-ready.png`；运行回执 `result.json` 保存在上述独立 dev 数据目录。

本机完整回归（核心恢复及活动横幅版本）：1095 passed in 172.32s。其后暂停文案优先级的最终增量已由上述 102 项针对性回归验证。提交 f34d8e3 为核心修复，5b81031 为本机发现的横幅修复及验收记录；均在独立 fix/feishu-read-retry 分支，未合入正在被另一会话修改的主工作区，未发布生产。
