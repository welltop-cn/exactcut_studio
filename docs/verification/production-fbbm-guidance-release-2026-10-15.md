# FBBM 失败提示正式发布 · 2026-10-15

用户授权 Grok review 通过后发布。审查见 [Grok记录](fbbm-error-guidance-grok-review-2026-10-15.md)。首次阻断已修复，Grok复核确认原阻断消除。

- 发布版本：us-east-20261015-fbbm-cf5344f
- 提交：cf5344f3cdb44fb41ad26458ba3b361f94134efd
- 镜像：sha256:b1d9ba457fd1678e47dbaaafa8c1363f159aa4ea8a94cc81c4f1c68c561480cb
- 前版：us-east-20261015-cut-67c063b
- 时间：2026-10-15T05:23:20 UTC，linux-ops publish status=ok。
- 修正版完整Linux回归：1337 passed、70 skipped，293.76秒；本地定向106项通过。
- 测试日志SHA：f4fce609af4ceb7120854a4c1e7a7b51c74548f34c4c6414dd92f240a8b9799b。

未发布旧候选c0b8254。切换前active_tasks=[]、phase_blockers=[]。正式配置不改动；配置和项目草稿共228份文件的发布前后SHA一致，快照在/srv/exactcut/preparation/pre-fbbm-cf5344f/hashes.json。

发布后公网healthz返回目标版本、status=ok、worker_alive=true、maintenance=false。正式登录后的app.js含“FBBM 交付失败”“重试交付 FBBM”；正式镜像的真实历史超时样本返回fbbm_quota，残留quota状态的中断返回interrupted。

指定项目419866e588fd4de59c9b1e6712e96a6f在验收时两条素材均已fbbm_ready、error_info=null；因此不再有失败提示可现场展示。本轮未提交任何真实交付或重试，不能把已就绪归因于文案修改。最初按仍失败断言验收发现状态已变化，随后按真实最新状态只读核验。

回滚沿用linux-ops上一版本，不回滚业务数据。测试实例未变更。
