# 剪切打码片段草稿

> 历史归档（2026-11-04）。正文保留当时的方案或执行状态，不代表当前版本；日常操作见[文档入口](../../README.md)。

- 保持单列表；新增侧栏保存时间、AI 片段菜单恢复识别结果。
- localStorage workbench.v3 的 schema_version 仍为 1，新增可选 aiEvents/clipEdits；旧草稿兼容读取，关联按原始边界一次绑定 eventId。效果键随关联迁移。
- 原始 AI 快照与编辑时间分离；时间保存同步 targets/deletions，预览与生成沿用原 API 合同。
- 取消选择保留编辑；恢复识别结果清除该片段时间/区域/效果及处理选择。放弃全部沿用服务端 discard 隔离旧结果，重开回到服务端 AI 结果。
- 重新识别期间列表可查看、编辑与生成禁用；仅成功时合并，失败保留。人工片段和编辑过的 AI 片段保留。新建议与受保护原片段时间交集达到较短区间 50% 时不重复加入；该匹配是时间启发式。
- 浏览器合成夹具检查菜单紧凑布局，未操作用户实际草稿。数据仍是本浏览器保存，不承诺跨设备同步。

验证：`PYTHONPATH=source ~/miniconda3/envs/py3.12/bin/python3.12 -m pytest source/tests/test_mosaic_ui.py -q`，52 项通过；`git diff --check` 通过。
