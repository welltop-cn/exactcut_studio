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
