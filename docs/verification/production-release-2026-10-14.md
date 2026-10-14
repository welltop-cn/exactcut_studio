# 正式发布验收（2026-10-14）

用户在测试实例完整验收后明确指示“正式发布吧”。本次通过正式发布管理器发布已审核、已测试的 main，未修改应用代码或业务配置。

- 发布时间：2026-10-14 10:36（北京时间）。
- 版本：`us-east-20261014-main-d7d8576`。
- 提交：`d7d857646133facd9ba9428600bfd67c3a2888b9`。与测试实例 a7ad9af 的应用实现相同，仅后续文档差异。
- 镜像：`sha256:617d8b9887c5706c4630a0086accad9f7717fa6b72c7e5af3c87a4d5ddc77c60`。
- 原版本：`us-east-20261012-timing-open-4e70327`。
- 正式制品目录：`/srv/exactcut/releases/us-east-20261014-main-d7d8576`。

## 构建与切换

从干净已提交 main bundle 创建独立服务器构建目录 `/srv/exactcut/preparation/release-d7d8576`，由 linux-ops 构建不可变镜像及隔离验证镜像。

本次 Linux 全套结果：**1272 passed, 48 skipped，264.41 秒**。测试日志 SHA256：`f4b1e7d2a87f8e8df9bce51762517219b570040037d9f4e16febe74f6565afc3`。

依照整合审查要求，将候选的 `source/deploy/linux_ops.py` 安装至稳定管理器；与封存源码逐字节比对一致。旧管理器备份：`/srv/exactcut/preparation/linux-ops-before-d7d8576`。镜像 `io.exactcut.editor-protocol=1`，管理器包含新编辑数据回退保护。后续不得使用旧管理器绕过保护：出现帧时间/片段效果数据后不允许旧镜像接管。

`check-switch` allowed=true、active_tasks=[]、phase_blockers=[]。正式切换使用 `linux-ops publish`，返回 status=ok。一次误用不存在的 `switch` 子命令被 CLI 拒绝，未执行切换；随后使用上述正确命令完成。未强停制作任务，未修改 Nginx。

## 发布后验收

- 公网 `https://autocut.creativefitting.ai/home/healthz` 返回新版本、status=ok、maintenance=false、worker_alive=true。
- 公网未登录 `/home/` 正常转登录页，200；受保护 app.js 未认证返回401，符合权限约束。
- 正式容器真实登录后，`/home/`、`/home/app.js`、`/home/api/me`、原项目详情均200；新帧输入代码存在，可视化 SourcePicker 不存在。
- 发布前195个 draft.json 已保存 SHA256，发布后全部一致，无缺失或变化。快照在服务器 `/srv/exactcut/preparation/pre-release-d7d8576-drafts.json`。
- 旧项目 `3daaa71df41b4b40b6b7b40e75c2ca2a` 仍为毫秒格式、complete、errors=[]；两条素材仍 fbbm_ready，成片SHA256与发布前一致。
- 正式配置仍 `/home`，OSS前缀 `exactcut-previews/production`，delivery_enabled=true；没有带入测试前缀或测试交付禁用配置。
- 发布后管理器 status 无活动任务、无交付阻塞、维护关闭。
- 独立测试实例仍 `test-main-a7ad9af`、health正常、worker_alive=true，未重启或改动。

本次发布包含帧时间填写/文本识别及后台索引、段字幕/闪白、20秒Hook、兼容和预览归属修复。可视化选帧与旧剪辑/马赛克实验未发布。正式环境本轮不创建新制作或交付任务；真实制作验收已在测试实例完成，见 [完整验收记录](test-instance-e2e-2026-10-13.md)。
