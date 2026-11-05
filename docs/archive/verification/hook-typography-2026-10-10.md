# Hook 字体、颜色、字号验收（2026-10-10）

> 历史归档（2026-11-04）。正文保留当时的方案或执行状态，不代表当前版本；日常操作见[文档入口](../../README.md)。

## 范围

为标准 Hook 编辑器及已有 composition 开头字幕增加内置字体选择、正文色、强调色、32～96 字号（1080 宽基准，随输出尺寸缩放），可恢复风格默认和自动字号。预览与成片复用相同渲染路径；设置变化使旧预览失效。指定字号无法在三行安全区内排下时返回校验错误。保留 schema 1。

## 验证

- 新增测试先运行：17 项失败，分别为缺少字段校验、未应用字体/字号/颜色、预览 identity 和 composition 字段丢失。
- 完成后测试：`source/.venv/bin/python -m pytest source/tests/test_hook_typography.py source/tests/test_phase1_hook.py source/tests/test_phase2_hook_workflow.py source/tests/test_phase2_preview_api.py source/tests/test_hook_visual.py source/tests/test_workflow.py source/tests/test_composition.py source/tests/test_phase3_hook_sampling.py -o addopts='' -q`，**87 passed in 23.13s**。
- `node --check source/exactcut/web/app.js` 与 `git diff --check` 通过。
- 8792 真实 HTTP save/preview：返回 200；衬线字体、60px、正文 #12AB34、强调 #EF2468，手动两行保留；预览像素已查看。
- Chrome 真实页面：字体改为无衬线粗斜体、72px、正文 #2878ff，点击保存并预览，返回 360px 预览图和保存成功提示，无校验错误。
- 恢复默认按钮：字体 auto、字号空白、两个颜色标为随风格、旧预览清除。随后测试项目被其他会话归档，未复原或修改该归档状态。
- production renderer 测试使用已确认的自定义预览，540 宽输出的字号为 24（1080 基准 48），颜色保持不变。

## 本地服务

用户指定 8792。原进程 43964，代码和数据目录为 source，environment=development，无活动任务。更新后进程 57028；健康检查返回 ok、development-hook-typography、worker_alive=true。

保留该实例的数据根与 `/tmp/exactcut-dev-concurrency.json`；显式采用 source/.env.development，不加载含外部服务配置的 source/.env。AI、外部下载和发布关闭。未部署生产。

浏览器入口：http://127.0.0.1:8792/ （应用跳转 /home/）。现有账号及数据保留。前端编辑文件中有其他会话并发修改（校验提示位置和滚动），本次未覆盖或提交它们。

## 字幕设置面板调整

根据用户验收反馈，将标准编辑器和 composition 开头字幕统一为「字幕设置」：风格在前，自定义字体与颜色折叠在同一面板内，位置/画重点/时长随后，底部统一「保存并预览字幕」。说明自定义覆盖优先级及切换风格保留自定义；已有自定义时自动展开。调整只涉及前端，无需重启后端。8792 浏览器检查确认仅一个设置面板、嵌套自定义区域、统一预览成功并返回保存提示。JS 语法和 diff 检查通过。

## 独立回归复核

2026-10-10：为排除本地开发数据环境标记以及其他会话正在进行的诊断功能修改，使用 `git archive HEAD source integrations operations` 的独立源码快照，仅覆盖本次 Hook 相关实现和测试。未复制凭据或开发业务数据。完整运行 `pytest tests -o addopts='' -q`：**794 passed in 104.60s**。此前快照遗漏 integrations/operations 导致的文件缺失失败，补齐后全部通过。

全量测试发现前端 reset typography 监听器追加在 app.js 末尾，会进入现有 test_usage 的源码切片；已移入 requests 事件处理区，单项与独立完整回归通过。最终 `node --check`、`git diff --check` 通过。再次读取 8792 `/healthz`：status=ok、release=development-hook-typography、worker_alive=true。独立快照通过不代表其他会话未完成的诊断功能修改也已通过全量回归。

Grok 审查发现 composition 丢换行及指定小字号提示不准确，均已复现并修复。修复后独立全量 **797 passed in 101.54s**；Grok 复审通过。8792 已重启为 PID 72166，真实保存与预览 HTTP 200。详见 [Grok 审查记录](hook-typography-grok-review-2026-10-10.md)。
