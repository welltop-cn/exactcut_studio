# Hook 预览输入失效修复（2026-11-10）

针对复盘问题 F2。本次只修改前端 Hook 预览回包与输入失效逻辑；没有改后端确认绑定、不可变版本、交付、OSS、持久 schema 或部署配置。尚未发布生产。

## 修复

- watcher 在等待响应前记录当前项目、素材输入和该素材的本地失效代次。完成结果只有在输入仍相同、代次未变、服务端 input_current 为真且没有未保存编辑时才能显示。
- 本地代次可识别“修改后又改回，再保存”的情况。它不依赖项目 revision，因此其他素材保存推进 revision 不会误拒当前素材的有效响应。
- 相同素材新任务开始后，旧任务回包不得覆盖新任务状态或预览。重复 watcher 不再提前覆盖当前任务登记。
- 结构化片段的集号、开始/结束时间输入调用已有 invalidateHook，清除显示缓存和旧预览凭证，并推进本地代次。

## 红绿证据

沿用 test_source_frame_ui 的独立 Chromium 页面和受控 API 边界，实际操作输入框和保存按钮；不连接业务服务、不使用生产数据。

1. 新增“修改 Hook 并保存后再放行旧响应”测试：原代码 **1 failed / 1.50s**，页面错误显示 1 个旧预览；修复后专题首轮 **4 passed / 1.22s**。
2. 新增结构化片段 episode/start_s/end_ms 输入测试：原输入分支 **3 failed / 0.88s**，旧预览仍存在；补齐 invalidateHook 后通过。
3. 扩展晚响应测试覆盖修改片段、修改后改回、其他素材保存；后者要求有效预览继续显示。
4. 补充新旧任务反序回包，要求新预览不被旧响应覆盖。

## 回归

指定 Python 3.12，从 source 执行，清除默认 marker 过滤：

```sh
/Users/matao/miniconda3/envs/py3.12/bin/python3.12 -m pytest -q -o addopts= \
  tests/test_hook_preview_draft_ui.py tests/test_source_frame_ui.py \
  tests/test_hook_preview_confirmation.py tests/test_hook_preview_source_identity.py \
  tests/test_hook_preview_snapshot.py tests/test_publish_session_ui.py \
  tests/test_publish_conflict_resync.py
```

结果 **93 passed / 39.23s**。随后新增新旧任务反序回包检查，最终 Hook 专题 **11 passed / 2.45s**；两批有重叠，不累加。`node --check exactcut/web/app.js` 通过。

本轮不是完整发布回归，也没有进行生产 Chrome 验收或真实外部上传。发布前仍需完整套件和正常发布门禁。复盘中的 F1 原片续传问题不在本次修改范围内。
