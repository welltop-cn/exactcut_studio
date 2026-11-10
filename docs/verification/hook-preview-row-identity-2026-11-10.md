# 删除素材后的 Hook 预览归属修复（2026-11-10）

复盘后续排查确认：两条素材分别有 Hook 预览时，删除第一条，剩余素材却显示第一条预览。原因是 hookPreviews 使用数组下标，而其他 Hook 任务状态按项目/request_id 识别。

产品修复统一预览缓存的写入、展示、失效和 AI 重点词转手工入口，使用项目 ID + request_id；页面 DOM 的行号继续只用于定位控件。

真实 Chromium 回归先失败：1 failed / 0.76s，断言明确显示期望 R2、实际 R1。修复后覆盖剩余素材有/无预览，以及删除后转手工选择继续使用 R2 的重点词。

补验手工选择时首次因测试未展开“字幕样式与显示”而超时；按真实操作展开后通过，没有绕过可见性或修改产品处理。

命令（source，指定 Python 3.12，清除 marker 过滤）：

```sh
/Users/matao/miniconda3/envs/py3.12/bin/python3.12 -m pytest -q -o addopts= tests/test_hook_preview_draft_ui.py tests/test_project_switch.py tests/test_publish_session_ui.py tests/test_publish_conflict_resync.py
```

结果 47 passed / 4.50s。没有生产访问、真实外部交付或后端改动；尚未发布。
