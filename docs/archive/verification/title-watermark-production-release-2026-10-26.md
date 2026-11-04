# 封面剧名与视频水印正式发布（2026-10-26）

> 历史归档（2026-11-04）。正文保留当时的方案或执行状态，不代表当前版本；日常操作见[文档入口](../../README.md)。

用户授权整理、提交后直接发布正式实例；没有发布测试实例。

- 版本：`us-east-20261026-title-b101a9a`
- 代码提交：`b101a9adddd66394b97dd793d79e084dab7f7d15`，功能提交为 `01afd3e`。
- 镜像：`sha256:537c21c1f3f33da7881fa360ef7aee2e07855f4add10113e76cdf20e875d0f44`
- 入口：https://autocut.creativefitting.ai/home/
- 上一版本：`us-east-20261021-thumbnail-f31a341`；数据 schema 仍为 1。
- 正式切换：2026-10-26 18:25（Asia/Shanghai）。

## 范围与验证

按剧 ID 获取封面，调用兼容图像编辑接口生成透明剧名；按剧复用、按批次固定资产。两个水印开关默认关闭，字幕位置不移动，重叠时字幕在水印上层。详见 [功能与配置](../../title-watermarks.md)。

发布整理额外将首页内联参考图移为按需加载、带版本缓存的静态资源，并保留未开启水印时的旧成片指纹。代码、原图资产、测试、配置模板和文档均已纳入提交；两份无关的未跟踪研究/历史验收文档未混入本次发布。

- 本地完整回归：1628 passed、10 warnings，374.54 秒。
- Linux 发布管理器隔离完整回归：1489 passed、139 skipped、10 warnings，334.06 秒；跳过不计通过。测试日志 SHA256：`093d443066ef049f9c269a4ac97f94126686d46a0e83df907a926100f2f9f2e5`。
- 初轮回归发现的旧指纹测试与 JS 测试隔离问题已修复；仅最终通过的版本封存并发布。
- 服务器使用候选正式配置只读查询 101607，成功取得剧名与 214675 字节封面，无 IP 覆盖。图像服务模型可用性已检查；真实图像提取与成片效果由此前本地两条素材验收覆盖，本次未重新制作线上素材或执行上传。
- 切换前 allowed=true、active_tasks=[]、phase_blockers=[]。
- 发布管理器健康检查通过；切换后公网与本机 healthz 均为 production、新 release、worker_alive=true、maintenance=false。
- 发布前后 761 份草稿、确认配置、素材状态、账号数据库与部署配置文件 SHA256 全部不变。

## 正式配置与回滚

`runtime.json` 仅新增 `title_extraction`，启用实时提取；现有模型/下载/上传配置保留。`production.env` 增加 KES 登录凭据，沿用正式图像接口配置。凭据不写入 Git、报告或命令输出，配置权限 0600；未复制本地 DNS 覆盖。

通过既有 linux-ops build/transition 发布。一次性配置适配器在 transition 的维护锁内、旧服务停止后原子更新配置；失败时先恢复原配置再启动旧服务。适配器的成功和失败恢复分支已用隔离文件验证。

服务器证据目录：`/srv/exactcut/preparation/title-watermark-20261026/`，包含 publish.log、integrity-before.json、integrity-after.json、publish.py，以及私有 rollback-config 备份。封存构建、测试和版本记录在 `/srv/exactcut/releases/us-east-20261026-title-b101a9a/`。

代码回滚使用 `linux-ops --root /srv/exactcut rollback us-east-20261021-thumbnail-f31a341`，保留现有业务数据。旧代码不使用新增 title_extraction 配置；如需同时撤销配置，使用上述私有备份并遵守停任务/维护锁流程，不恢复旧数据库。
