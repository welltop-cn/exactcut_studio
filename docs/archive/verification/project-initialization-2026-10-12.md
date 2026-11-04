# 新剧目初始化修复与验收（2026-10-12）

> 历史归档（2026-11-04）。正文保留当时的方案或执行状态，不代表当前版本；日常操作见[文档入口](../../README.md)。

## 原因与修改

生产版本 `us-east-20261011-review-d8cdd5b` 的新版 confirm → materialize → fetch 路径未经过旧 `_start_batch` 的剧目初始化。101614、101615 在真实 `validate_manifest_sources` 读取 project.json 时失败。

提取公共 `initialize_project`，旧确认与下载入口共同调用。在项目级 flock 下创建目录和缺失配置，已有配置保持不变，仍执行原有剧目身份、剧集授权与媒体校验。

发布工程提交：`18771e707b2d7461239998f75f3d8752ec4f1973`；主工作区对应 cherry-pick：`6dccad7`。

## 测试

- 新回归先复现 fetch 返回 2，修复后返回 0；保留真实的 validate_manifest_sources 和源文件解析，仅替换媒体探测。
- 覆盖已有授权限制不被覆盖、并发幂等初始化、项目软链接拒绝。
- 定向 99 passed；本地完整回归 1030 passed，159.97 秒。
- 独立代码审查无阻断项，另行运行定向 99 项通过。

此前漏检原因：test_round1_integration.chain 将 validate_manifest_sources 整体替换为空函数；部分旧成片夹具先调用旧 start_batch，预先创建配置。调度链路通过不代表真实项目初始化路径已覆盖。

## 发布前运行状态

两条 fetch 已写入 exit_code=2 的失败阶段结果，但在退出前处于 T 状态。核验原进程令牌、PID/PGID、实例、失败 runtime、阶段指纹与父项目未暂停后，通过现有 signal_identity 发送 SIGCONT，使其完成退出；未重放制作、未修改任务记录。随后队列恢复推进。此为运维处置，本次初始化补丁未修改终态子进程暂停逻辑。

## 发布与线上测试

- Linux 完整回归：1023 passed，7 skipped；管理器构建成功。
- 2026-10-12 16:19:51 北京时间发布 `us-east-20261012-project-init-18771e7`，镜像 `sha256:87b2e6ba876ef3c59cfc9f02a161bf96d0cfefdee1df5e3ccf1b0ec8d7b1a1b0`。切换前 active_tasks、phase_blockers 为空；切换后再次 healthz 确认 production、目标版本、maintenance=false、worker_alive=true。
- 按用户要求，复用原项目配置建立新项目 `2b49b3f61ee64c88bcaa889110bfdd12`，归属 `hookqa212220a`，以该用户会话通过 HTTP start 提交（202），临时会话随后注销。
- 使用现有 clone_batch 分配新 ID 与序号，核对排除身份字段后的制作配置一致，并检查无已保留文件名及素材 ID 冲突。素材为 `R22a64e7da27d4cc69c2bc82393d20841`（序号 3）、`Rad9c5f6576944ecd972db0d6e9e93dda`（序号 4）；原项目未重试。
- 线上自动创建 101614/project.json；两个 fetch 均完成，随后进入真实 base_cut。素材时间线分别约 524.077 秒、628.052 秒；编码进度持续推进。此证据确认原缺配置故障修复，不能代替完整成片验收。
- 私有创建与提交证据：服务器 `/srv/exactcut/data/.web-state/acceptance/project-init-20261012.json`。

最终验收完成：两条均为 current / qc_passed，父项目 review，候选版本为空；以 hookqa212220a 临时会话验证两条预览 Range 请求均返回 206 和 4096 字节，随后注销。主视频及预览文件 SHA256 与记录一致，均为 1080×1920、30fps 且有音频。实际时长分别 524067 / 628100 毫秒，与计划相差 -10 / +48 毫秒，QC 无错误。未提交交付。

完整验收私有证据：服务器 `/srv/exactcut/data/.web-state/acceptance/project-init-20261012-result.json`。用户未暂停，本次未执行任何自动恢复操作。
