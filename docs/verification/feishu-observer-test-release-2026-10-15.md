# 飞书自动化交付：测试实例发布

- 应用代码：`b00374a`；测试夹具修正：`a50b85b`。两者应用目录完全一致。
- Grok 审查阻断已修复并复核通过，见同目录审查记录。
- 最终 HEAD 完整 pytest 退出码 0，1441 项；日志 `/tmp/exactcut-observer-final-head.log`。包含实际子进程退出及恢复测试。
- 只部署 `exactcut-test`，版本 `test-observer-b00374a`。公网 `/test/healthz` 健康、maintenance=false、worker_alive=true；登录后项目和成片页正常，显示确认上传 FBBM，无交付禁用提示。
- 用户明确授权测试实例使用真实 OSS/飞书配置，覆盖此前测试环境禁止真实服务凭据的约定。本次复制仅写测试配置；未复制 CF_ADS 凭据。publish/external writes=1，delivery_enabled=true，预览 namespace=test 不变。
- 按实际服务初始化方式加载 env 后，真实飞书 API 列字段通过：剧ID/来源/ADS素材ID/视频ID type=1，OSS_URL/封面URL type=15，状态 type=3。未创建记录、修改字段或提交素材。
- 新建行只写剧ID、OSS_URL、状态=待处理、来源；后续只读取飞书结果。复用既有 OSS_URL 行不更新记录。
- 测试代码及配置备份：`/srv/exactcut-test/backups/observer-b00374a`、`/srv/exactcut-test/code-observer-b00374a.previous`。私有部署报告 `/srv/exactcut-test/deployments/test-observer-b00374a.json`。
- 正式服务前后均为 `us-east-20261015-fbbm-cf5344f`，启动时间 `2026-10-15T05:23:21.533169538Z` 未改变，健康正常；未改正式配置、数据或代码。
- 实际写飞书并等待外部自动化完成的端到端操作未执行，留给用户测试。历史未知写入回执不迁移。

## 2026-10-15 私有成片命名空间修复

测试交付失败“私有成片回执与本次确认的版本不符”源于 OssPublisher.upload 硬编码 production 前缀，而实际 preview.namespace=test。提交 0b13f2f 改用已有 object_prefix()，仅接受实例配置命名空间；bucket/endpoint/SHA/bytes/HEAD/etag 保护不变。Grok复核不阻断，完整回归1442 passed（10 warnings）。

已仅部署测试实例 test-observer-0b13f2f，health=ok、maintenance=false、worker_alive=true。正式版本和启动时间未变。备份 /srv/exactcut-test/backups/observer-0b13f2f 及 code-observer-0b13f2f.previous。

对用户项目 ff64a2f2d4d64bbaacf9216323624b80 的真实148秒成片，通过只读包装调用原upload校验：实际HEAD源对象并核验元数据，在copy_object执行前拦截，返回copy_source_validated_no_write；按OSS_URL查飞书无记录。没有上传、修改回执或创建飞书行。旧失败为preparation_failed且无飞书ID，可通过正常重试入口继续；未代用户重试。正式namespace默认production及跨namespace拒绝均测试通过，后续正式发布仍需独立授权和部署检查。
