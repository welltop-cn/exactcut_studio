# 告警项目链接修复验收

## 问题与修改

纳管项目的内部子批次不能作为制作人工作台链接。执行阶段诊断上下文新增 `parent_batch_id`，通过现有 `material_workers.parent_batch_id()` 读取；普通项目填自身，原 `batch_id` 保留。HTTP 项目上下文同样记录自身项目 id。

监控优先使用有效的 `parent_batch_id` 生成「项目」链接及「项目 id」，与 `batch_id` 不同时显示「内部批次」。旧日志没有父项目字段时沿用原行为；无效父项目字段回退到原批次。

## 验证

- 修复前：有父项目的告警回归失败，无父项目的回归通过。
- 命令：`cd source && /Users/matao/miniconda3/envs/py3.12/bin/python3.12 -m pytest tests/test_prod_monitor.py tests/test_material_versions.py tests/test_diagnostics.py`
- 默认层结果：128 通过，1 跳过，58 未选择（默认不执行 UI / integration 层）。
- 独立 worktree 扩展验收：上述三个文件加 `tests/test_concurrency_recovery.py`，使用 `-m ''` 执行所有相关层，227 通过，1 跳过。
- 覆盖真实监控告警文本、内部子批次与普通项目的执行诊断上下文，以及原有脱敏和监控行为。

## 本地 dev 链接验收补充

使用独立 worktree 的告警修复代码 `665ab8a`，只读读取本地 8793 dev 已完成项目的真实父子身份，通过真实 `stage_results.run_child` 写入受控诊断事件，再使用监控 `event_context` 生成告警上下文；诊断输出写到独立证据目录，没有执行制作或发送飞书。

- 内部批次：`1fc5de6f5f7b480fad84049f9f58c7be`。
- 父项目：`5a23f54ac32d4bffa6084869bf6d5623`。
- 生成链接：`http://127.0.0.1:8793/home/#5a23f54ac32d4bffa6084869bf6d5623`；“项目 id”也是父项目，“内部批次”保留 child。
- Chrome 实际打开该链接，显示父项目及两条成片，未进入内部批次页面。该网页服务运行的是独立 AI 重试分支，告警诊断与格式化来自本分支；两分支没有合并。
- 私有脚本和 JSON 证据：`/Users/matao/exactcut-test/alert-parent-665ab8a/verify_alert_context.py`、`alert-browser-evidence.json`。

只进行隔离本地测试；未发送飞书消息，未合并 main，未部署。
