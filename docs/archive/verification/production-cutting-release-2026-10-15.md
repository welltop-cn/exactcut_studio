# 正式剪切发布 · 2026-10-15

> 历史归档（2026-11-04）。正文保留当时的方案或执行状态，不代表当前版本；日常操作见[文档入口](../../README.md)。

用户明确授权正式发布，并要求确认没有制作任务。首次检查有两条 FBBM poll 队列；等待期间仅构建独立候选。切换前管理器 active_tasks=[]、phase_blockers=[]、check-switch.allowed=true，未忽略任务、未手改业务状态。

- 版本：us-east-20261015-cut-67c063b
- 提交：67c063b6076121f87374f9fecbd41d831bd6a7c4
- 镜像：sha256:bd4f71773584ccedc3b08ca3fbeca18476689bbfff4e759f8761fbfa87d0a268
- 前版：us-east-20261014-main-d7d8576
- 发布管理器切换时间：2026-10-15T04:48:50 UTC，status=ok。
- 隔离 Linux 全套回归：1335 passed、70 skipped，293.39 秒。跳过项不算通过；真实测试实例浏览器验收见 cutting-101606-server-2026-10-14.md。
- 测试日志 SHA：5a87eae4688a7281502fb821811a5aa9b45c4e15e60f8c5d8dfb2b54ac427569。

通过 linux-ops build/publish 发布干净提交；未携带本地未跟踪实验文件。runtime.json 仅增加完整 cutting 配置并启用 scan，production.env 增加已在测试验证的 Gemini 凭据和 EXACTCUT_CUT_SCAN_ENV=/run/exactcut/production.env，文件维持0600；无密钥入库。

公网 https://autocut.creativefitting.ai/home/healthz 验证目标版本、production、worker_alive=true、maintenance=false。正式实例内登录后首页、cutting.js、api/me 均200；按钮为「剪切」。cutting.enabled、scan.enabled 和凭据读取通过；应用解析的 OSS 前缀为 exactcut-previews/production，delivery_enabled=true，保留原有业务配置。

223份发布前项目草稿 SHA 全部不变。发布后管理器 active_tasks=[]、phase_blockers=[]，维护关闭。未在正式项目主动生成剪切或提交交付；本轮是发布与只读验收。

配置和发布前草稿哈希备份：/srv/exactcut/preparation/pre-cut-67c063b/。回滚使用 linux-ops，保留当前业务数据；如需撤回配置，恢复该目录中的配置备份，不能回滚数据库。独立测试实例未重启。
