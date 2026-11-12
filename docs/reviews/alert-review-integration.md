# 三分支集成与告警审查修补

日期：2026-11-12。候选分支 `fix/alert-review-integration`，基于 `main` 的 `fd2e508` 集成以下三个固定分支头：

| 原分支 | 集成提交 | 保留的主要行为 |
| --- | --- | --- |
| `fix/hook-validation-auto-retry` | `76a077e` | 单条 Hook 校验拒绝自动重试、拒绝证据、成功素材保护及相关诊断 |
| `fix/alert-parent-project-link` | `77ac0d9` | 阶段及 HTTP 父项目上下文，外部监控优先父链接并标注内部批次 |
| `feat/application-alerts` | `d446586` | 默认关闭的持久应用告警、Webhook 门禁、后台发送及外部 scan_logs 开关 |

集成提交为 `bd1b44a`；后续修补保存在该候选分支。main 和原有分支未改动；未更新运行中的本地验收树、生产配置或 cron，也未发送真实飞书消息。后续合入应包含候选分支的防护与文档修补提交。

## 父项目诊断查询故障隔离

原 `stage_results.run_child()` 在调用业务操作前查询 `material_workers.parent_batch_id()`。查询损坏或不可读 draft，或 media_internal 结构不正确时，异常直接向外传播。

新增回归先复现 18 项失败：六种故障分别经过 fetch/render/upload 的调用参数路径。故障包括坏 JSON、顶层数组、字符串 media_internal、缺少父字段、非字符串父字段，以及文件读取 PermissionError。

修补只围绕父查询：先保留 batch_id；单独捕获 OSError、ValueError、TypeError、AttributeError、KeyError，且只将字符串父 id 放入诊断上下文。业务操作在该捕获范围之外执行，其自身异常照常传播。有效父项目及普通项目上下文保持原行为。

回归验证的是诊断故障不会阻止操作被调用，不声称真实 fetch/render/upload 可以忽略自身业务所依赖的损坏草稿。没有更改业务校验、重试资格、返回码、权限或持久业务协议。

## 合并交叉点

已查看最终差异并运行组合回归：

- `webserver.py` 同时保留父项目 HTTP 上下文、重点拒绝排障字段及应用告警发送器生命周期。
- `error_guidance.py` 同时保留重点校验的用户提示与 WEBHOOK 环境秘密值遮盖。
- `monitoring/prod_monitor.py` 同时保留父项目链接、内部批次标注与 scan_logs 开关；新增一条组合回归，先关闭扫描确认无消息，再打开扫描确认消息链接到父项目。
- `test_material_versions.py` 保留原自动重试及成功素材保护测试，并新增诊断故障隔离与业务异常传播回归。
- 其他监控、应用告警与用户错误提示测试均保留。

## 公开文档路径

本次三分支改到的 Markdown 文档已去除个人主目录的绝对路径：Python 命令写为 `python3.12`，实际解释器由执行环境选择；私人验收脚本/日志/截图根写为 `${PRIVATE_TEST_ROOT}`，文档明确它是占位符、不是公开仓库文件或预设变量。

清理包括应用告警说明、父项目链接验收、Hook 自动重试与 Chrome 验收，以及本地服务清单（含该清单中原有的同类路径）。保留具体文件名、固定代码提交和已有验收边界，不将私有脚本描述为已随仓库分发。其他历史归档未做全仓公开发布审计，不能据此宣布整个仓库已完成公开检查。

## 验证

使用配置的 Python 3.12 在独立工作树执行，命令在公开文档中写为可移植入口：

```sh
cd source
python3.12 -m pytest -o addopts='' -q -ra \
  tests/test_application_alerts.py tests/test_prod_monitor.py \
  tests/test_material_versions.py tests/test_diagnostics.py \
  tests/test_interactive_queue.py tests/test_mosaic_queue_release.py \
  tests/test_interactive_main.py tests/test_hook_emphasis.py \
  tests/test_intra_project_results.py tests/test_public_errors.py \
  tests/test_concurrency_recovery.py
```

- 相关十一文件，显式执行全部层：**377 通过、1 跳过**，34.52 秒。唯一跳过为本机没有 setfacl/getfacl。
- 新增故障用例与有效父查询共 19 项快速回归通过；业务异常不被捕获及监控开关与父链接组合回归包含在上述 377 项内。
- 完整默认 unit：**1,703 通过、1 跳过、1,347 未选择**，160.77 秒；未选择的是默认排除的 UI/integration，不能将默认层描述为全仓所有层。
- `git diff --check` 通过；本次改动涉及的 Markdown 逐行检查未发现个人主目录绝对路径。
- 只使用独立临时目录与模拟通知边界，不访问真实模型、飞书、OSS 或 FBBM。

## 差异审查

相对三个已审分支的集成提交，本次生产代码只修改 `stage_results.run_child()` 的可选诊断查询。异常捕获范围没有包住 operation、stage_enter、stage_finish 或阶段证据写入；没有削弱这些业务操作的校验。已通过 git diff 检查所有交叉修改，未删除鉴权或对外写入开关。文档修改不改变运行配置。

范围内未发现剩余的高危/严重问题。生产启用、旧 outbox 排空、应用告警开启、日志扫描与 root feed 停用，仍按[应用告警切换说明](../application-alerts.md)单独授权。外部健康与资源监控继续保留。此报告不替代真实群接收验收及全仓公开检查。
