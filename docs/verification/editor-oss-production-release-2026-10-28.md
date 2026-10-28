# 编辑器 OSS 正式成片播放：正式发布

用户授权提交、发布真实实例并关闭测试实例。2026-10-28 21:23（Asia/Shanghai）通过既有 linux-ops 发布。

- release：`us-east-20261028-editor-oss-33b0d1b`。
- 提交：`33b0d1b79bd63945257754d6c9e425e970ca604b`，应用代码与线上测试验收的 `0a3825b` 一致；后续提交仅记录测试证据。
- 镜像：`sha256:5bcff2437700b40a01d78c5552c2b8a11f9cb3658dc6c8dd5639cad3c9201a5f`。
- 前一版本：`us-east-20261027-playback-a5f7633`。
- 入口：https://autocut.creativefitting.ai/home/ 。已有页面需刷新并重开编辑器。
- 已提交功能涵盖正式原片 OSS 只读签名及有界恢复、准确帧短时竞争等待、识别阶段诊断。无关未提交 app.js/index.html 和文档未纳入。

## 门禁与配置

干净提交本地全量 1723 passed、1 skipped。Linux 发布管理器隔离回归 **1531 passed、193 skipped、10 warnings，344.45 秒**，退出0；跳过不算通过，浏览器能力已在本地/测试实例验证。

- Git bundle SHA256：`43a97245b11e127c0fc059eb908c63e24aa8a56aaa1a10f07edf703753089f5f`。
- Linux 测试日志 SHA256：`f029090258d396e5026f6f3841912483181e327b8a1ebaa21c78e7aafd65aeef`。
- check-switch：allowed=true、active_tasks=[]、phase_blockers=[]；管理器在维护屏障内再次检查并发布。
- 正式 runtime 仅新增 `cutting.oss_playback_enabled=true`。对备份做结构比较，其他配置值不变；deployment.json、production.env SHA256 未变。运行时确认 OSS enabled=true、namespace 为 production。
- 原配置备份：`/srv/exactcut/preparation/release-editor-oss-33b0d1b/runtime.previous.json`。
- 数据 schema 保持1；没有重跑素材、替换成片或提交外部交付。

## 切换后核验

公网与服务器本机 health 均为新release、status=ok、worker_alive=true、maintenance=false。

Chrome 已登录工作台加载新 editor-source.js。最初反馈素材 R91f0aaa0 当前已经入库，保留不可编辑限制；未绕过权限。改用已有可编辑会话只读请求 source-url：200、source=oss、reason=verified、Cache-Control=no-store，主机为 aigc-us02-content.oss-us-west-1.aliyuncs.com。

服务器使用同一只读签名逻辑抽样 GET Range，返回206/1024字节，与当前正式 master 前1024字节相同；没有补传对象。浏览器验收标签页处于后台，本轮媒体未完成解码/播放验证，不能将其记录为正式环境播放通过；连续播放、定位、恢复和前后对比证据见[测试实例验收](editor-oss-test-deployment-2026-10-28.md)。自建标签页已关闭。

测试实例停止前未发现 queued/running 记录；执行专用 control stop 后确认 `exactcut-test=exited`，账号、配置、视频与测试结果保留。停止后正式 health 仍正常。

## 回滚与证据

封存目录 `/srv/exactcut/releases/us-east-20261028-editor-oss-33b0d1b/`；发布日志、check-switch、配置备份与对比在 `/srv/exactcut/preparation/release-editor-oss-33b0d1b/`。

代码回滚使用 `/srv/exactcut/bin/linux-ops --root /srv/exactcut rollback us-east-20261027-playback-a5f7633`，仍须通过任务排空和兼容性门禁，不回滚业务数据。播放开关可单独关闭；已发签名900秒有效，关闭只影响后续解析及重开编辑器。恢复完整旧配置前必须核查是否有后续配置修改，不能覆盖其他变更。发布管理器保留健康失败自动恢复上一镜像机制，本轮未触发回滚。
