# 告警项目链接修复验收

## 问题与修改

纳管项目的内部子批次不能作为制作人工作台链接。执行阶段诊断上下文新增 `parent_batch_id`，通过现有 `material_workers.parent_batch_id()` 读取；普通项目填自身，原 `batch_id` 保留。HTTP 项目上下文同样记录自身项目 id。

监控优先使用有效的 `parent_batch_id` 生成「项目」链接及「项目 id」，与 `batch_id` 不同时显示「内部批次」。旧日志没有父项目字段时沿用原行为；无效父项目字段回退到原批次。

## 验证

- 修复前：有父项目的告警回归失败，无父项目的回归通过。
- 命令：`cd source && /Users/matao/miniconda3/envs/py3.12/bin/python3.12 -m pytest tests/test_prod_monitor.py tests/test_material_versions.py tests/test_diagnostics.py`
- 结果：128 通过，1 跳过，58 未选择（默认不执行 UI / integration 层）。
- 覆盖真实监控告警文本、内部子批次与普通项目的执行诊断上下文，以及原有脱敏和监控行为。

只进行隔离本地测试；未发送飞书消息，未合并 main，未部署。
