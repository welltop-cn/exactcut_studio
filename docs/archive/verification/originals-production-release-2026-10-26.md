# 原片下载正式发布（2026-10-26）

> 历史归档（2026-11-04）。正文保留当时的方案或执行状态，不代表当前版本；日常操作见[文档入口](../../README.md)。

用户授权测试通过后提交，并直接发布正式实例。没有发布测试实例。

- 版本：`us-east-20261026-originals-0029b0d`
- 代码提交：`0029b0d907f916f3f63f4d93b79693465f799500`
- 镜像：`sha256:87a3ec7732c563988fc0514b305ae0b2ce182b558be6928e2f4a9fd6a7edac84`
- 入口：https://autocut.creativefitting.ai/home/
- 上一版本：`us-east-20261026-title-b101a9a`；数据 schema 仍为 1。
- 正式切换：2026-10-26 21:17（Asia/Shanghai）。

## 范围与验证

剧号输入框旁可以下载阿里云上已有的原片。列表来自对象名，未上架的集只加一个标记。浏览器选定目录时走同源文件流；Safari 和 Firefox 仍用临时签名地址。签名约 15 分钟，对象保持私有。

这次镜像同时带上此前只在本地的两个提交：`6096ca9`（确认制作时恢复未提交草稿）和 `359e5ae`（封面水印发布记录）。两份无关的未跟踪研究/历史验收文档没有进入提交。

- 本地完整回归里，马赛克界面一条点击在 3 秒内超时；按钮当时可见且可点。该条和原片测试随即重跑通过。Linux 发布管理器隔离回归：1503 passed、139 skipped、10 warnings，336.57 秒。测试日志 SHA256：`301ba7cfa09a715b8cb75201da353a4a57934c3a5b2cba2b586f78155cda1349`。
- 切换前 allowed=true、active_tasks=[]、phase_blockers=[]。
- 切换后公网与本机 healthz 均为 production、新 release、worker_alive=true、maintenance=false。
- 未登录访问原片列表返回 401。容器内只读列出剧 101606 的 EP1–EP30，30 集都已上架，列表不含签名地址。EP1 签名为公网 HTTPS、900 秒；Range 请求返回 206 video/mp4。没有下载整集，也没有制作或上传成片。

## 正式配置与回滚

`production.env` 增加上架查询所需的三项 Metabase 配置。内容桶和公网 endpoint 沿用现有值。源桶未单独配置，程序使用默认 `aigc-us02`。发布、AI、外部写入和正片下载开关保持原值。凭据不写入 Git、报告或命令输出；配置权限 0600。切换前的配置备份在 `/srv/exactcut/preparation/release-originals-0029b0d/production.env.before`。

服务器证据目录：`/srv/exactcut/preparation/release-originals-0029b0d/`。封存构建、测试和版本记录在 `/srv/exactcut/releases/us-east-20261026-originals-0029b0d/`。

代码回滚使用 `linux-ops --root /srv/exactcut rollback us-east-20261026-title-b101a9a`，保留现有业务数据。旧代码不读取新增的 Metabase 配置。
