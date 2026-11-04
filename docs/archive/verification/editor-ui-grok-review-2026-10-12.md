# 编辑器 UI 分组回归与 Grok 复审 — 2026-10-12

> 历史归档（2026-11-04）。正文保留当时的方案或执行状态，不代表当前版本；日常操作见[文档入口](../../README.md)。

范围：开头字幕、剧情片段、结尾画面的分区，字幕与校对折叠，结尾时长并排，时间线写法说明移动。没有发布、修改生产或提交外部物料。

## 已确认并修复

1. 校对详情原先关闭时，增量刷新用旧 false 覆盖新错误的 open。浏览器回归先失败后通过；现在保留模板强制展开或用户已展开状态。
2. source_status=failed 但无 s.error 时，默认折叠隐藏下载失败。现在自动展开失败，并在摘要显示失败数；其他已定义的等待状态和空时长在摘要显示待核对数量，保持紧凑。
3. 普通字幕修改后原有逻辑会清空手工重点，默认折叠使变化不明显。现在手工模式初次展示展开，修改字幕后展开并提示重新选词。没有修改清空范围的原有规则。

## 验证

- 最终隔离副本全量：1129 passed in 189.82s，退出码 0。
- 三种模式的浏览器用例：片段表单、composition、文本时间线；核对字体、字号、开头时长、结尾时长与引导语，折叠后完整 draft 提交，回填值不变，只读禁用，说明所在分区，手工重点提示，无页面脚本错误。保存接口在该浏览器用例中为桩，后端保存与渲染由既有全量测试覆盖。
- 校对回归覆盖增量更新后错误/下载失败可见、待核对摘要、原有展开状态与焦点保持。
- node --check、git diff --check 通过。
- 初次工作区回归有两项 development/production 标记冲突；未删除用户环境标记。隔离副本准备时曾漏带 integrations 和 operations 的跨目录依赖，补齐受版本控制文件后全量通过。

## 独立审查

Grok CLI 请求 grok-4.7 / high，实际 modelUsage 为 grok-4.7-build；会话 01a12fb6-0699-7cf2-991c-cd71e4619632。初审与复审均 exit 0、stopReason=end_turn、非空完整报告。通过独立临时 cwd 提供源码，禁用工具、Web、子代理和 memory；未提供凭据、项目数据或环境文件。Grok 做静态审查，浏览器与测试由本代理执行。

低风险说明：保存、添加片段等整卡重绘后，普通模式的样式详情仍会收起；已写入 draft 的值不受影响，手工模式展开。没有承诺持久化所有用户展开状态。

### Grok 修复复审原文

三项中风险均已消除。补充的 helper 没有改错这三项对应的业务规则，三类草稿测试也盖住了隐藏字段、时长换算和只读禁用。

时间校对：`details.open = details.open || open`。模板只在有 `s.error` 或 `source_status==='failed'` 时带 `open`，新错误和下载失败会展开，原先已展开的不会被盖掉。`missing`、`waiting`、`queued`、`downloading`、`checking` 或时长为空，只在摘要追加「N 段待核对」，不强制展开。`withEndingTiming` 在模式不为 `none` 时把 `duration_ms` 计入 `planned` 与 `known`，折叠摘要含片尾；`none` 则为 0。

手工重点：非成片且 `mode==='manual'` 时初次渲染展开。改 `hook_text` 仍清空 `manual_ranges`，并打开面板、提示重新选词。成片开头不走这里。

结尾：有 `composition` 时写入 `composition.ending`，否则写入 `row.ending`；`endingOf` 仍先读 `row.ending`。测试里的三种行都只有一边，因此保存位置正确。成片开头时长是秒，字幕与结尾停留是毫秒。保存后的整卡折叠仍是呈现行为。

## 最终前端文件指纹

- `source/exactcut/web/app.js` SHA-256 `330ebd85d63bf9e1ed75d49ac80b54f17fa1b6410953d6ddc8a43808ac29ebeb`
- `source/exactcut/web/index.html` SHA-256 `b337cb5cb33fd9074dd035ad6caa2f90b020a78c606225bf9983677e33e12801`
