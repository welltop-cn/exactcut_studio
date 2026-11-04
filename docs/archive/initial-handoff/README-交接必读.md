# Exact Cut FBBM 开发交接包

> 历史归档（2026-11-04）。正文保留当时的方案或执行状态，不代表当前版本；日常操作见[文档入口](../../README.md)。

打包日期：2026-10-08（北京时间）。线上基线 `v0.3.3-phase4`，Git 提交 `23e303c044c24fe7d0ffb2ab5a4dcf7ea74705ec`。

这是完整工程源码与开发交接资料，供开发继续稳定化；本次没有修改、发布或重启线上服务，也不代表已完成新平台迁移或长期稳定性验收。打包时本机生产健康检查正常，工作目录回归 213 项通过；打包副本的独立测试见 evidence/package-validation.json。

## 内容

- `source/`：所有 Git 跟踪的前后端源码、自动化测试、Excel 模板、示例、锁定依赖、配置示例及原有文档。
- `history.bundle`：所有 Git 分支和标签的离线历史，可恢复提交脉络。
- `operations/`：当前实际运维控制器和入口原文；入口有原机绝对路径，仅供迁移参考。
- `integrations/drama-material-hub/scripts/`：应用实际依赖的物料中心三个 Python 脚本快照。
- `configuration/`：无真实密钥的环境配置样例。
- `evidence/`：版本、健康检查、源码对照、提交记录、本地文档差异、测试和打包校验信息。
- `MANIFEST.sha256` 与 `verify_package.py`：包内逐文件完整性校验。

源码按当前开发工作树复制，应用代码与线上版本一致。只有 `docs/archive/source/docs/一期修复验收.md` 有尚未提交的两行差异，已保留在 `evidence/working-tree.patch`；不擅自提交或丢弃该修改。Git bundle 恢复的是提交内容，要保留该文档修订可在克隆后应用此 patch。

工程包不含 `.env`、登录密码、生产账户数据库、会话、批次/任务状态、用户 Excel、原片/成片、反馈、物料同步去重账本或虚拟环境。模板和 Git 中既有示例 Excel 已包含。完整接管既有业务仍需私下交接密钥和单独迁移最新业务数据；不可把此包当作业务数据备份。

## 开发启动

以下命令在本包根目录执行，面向 macOS。需要 Python 3.11、FFmpeg/ffprobe；线上记录为 Python 3.11.15、FFmpeg 8.1.1，Python 依赖见锁定文件。

```bash
python3 verify_package.py
cd source
./bin/setup
.venv/bin/python -m pytest
./bin/dev-web
```

开发页为 `http://127.0.0.1:8792`，初始用户 `admin`，随机密码写入开发副本 `.web-state/password`。dev-web 使用独立数据目录并关闭执行器和外部调用，便于页面开发；完整制作以隔离 fixtures / mock 验收。不要复制生产 `.env` 到开发目录。

需要保留 Git 历史时，在包根目录另行执行（不能以 source 作为已存在的克隆目标）：

```bash
git clone history.bundle exact-cut-fbbm-dev
cd exact-cut-fbbm-dev
git switch -c handoff-stability 23e303c044c24fe7d0ffb2ab5a4dcf7ea74705ec
git apply ../evidence/working-tree.patch
```

源码内 AGENTS.md 记录的是原机目录边界；接手开发应使用自己的隔离开发目录。原 README 含早期说明：现行生产端口为 8791（不是旧 8790）；Hook 已支持三行、AI 重点分析/手工标记；最新行为以代码、tests、docs/一期修复验收.md 和 docs/开发发布与回滚.md 为准。

## 业务流程与工程入口

Excel 导入 → 解析与时间核对 → 编导及清单确认 → 正片补齐 → Hook 预览/确认 → 精确剪辑与技术 QC → 成片预览 → 用户勾选上传 → OSS / 飞书 / CF-Ads → FBBM ready 且有视频 ID。

| 模块 | 位置 |
| --- | --- |
| 页面、HTTP、账户鉴权 | exactcut/web/、webserver.py、accounts.py |
| 导入/清单/时间解释 | intake.py、compiler.py、timecode.py、timing_review.py |
| 批次执行/恢复/暂停 | workflow.py、batch_control.py、progress.py |
| 渲染/Hook/QC/空间管理 | renderer.py、hook_*.py、qc.py、storage.py |
| 物料候选/下载 | catalog.py、material_download.py、外部 Material Hub |
| OSS/飞书/FBBM | publish.py |
| 环境隔离与发布 | runtime.py、deploy/ops.py |

用户数据集中于独立数据根目录的 `.web-state/`、`projects/`、`feedback/` 和 `.env`。当前数据 schema 固定为 1。上传确认绑定已预览视频 SHA-256，续查应复用远端记录；代码回滚只切换代码和依赖，不能恢复旧业务数据库或重复上传。

进一步部署与稳定化说明见 `docs/archive/initial-handoff/部署与稳定化交接.md`。
