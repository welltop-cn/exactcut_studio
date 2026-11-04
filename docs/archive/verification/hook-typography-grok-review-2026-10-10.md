# Hook 字幕设置 Grok 审查（2026-10-10）

> 历史归档（2026-11-04）。正文保留当时的方案或执行状态，不代表当前版本；日常操作见[文档入口](../../README.md)。

- 请求模型：grok-4.7；实际返回：grok-4.7-build；reasoning effort：high。
- 会话：0b5d6786-0c2c-45d3-b7f6-c56c5d05e92f。
- 只读独立材料目录；禁用 shell、编辑、联网、MCP 和子代理，不提供凭据或业务数据。
- 首轮材料 SHA256：d6046dbad37c342e80add3027447fdfe94e09726c062804f1d75d28b7cc79112。长输入截断后补充独立源码文件；两次达到轮数上限的中间回复不计作完整审查。最终报告及修复复审均 exit 0、stopReason=end_turn。

## 核实与修复

1. composition 开头字幕把换行替换为空格，与新面板手动换行说明冲突。新增测试复现，改为删除 CR、保留 LF；测试检查保存字段以及渲染两行。
2. 指定小字号仍提示精简文案或调整换行。新增测试复现；指定字号时改为建议调大字号，自动字号保留原提示。验证 32 和 48 两个阈值场景。
3. 自查完整测试发现 reset typography 监听器位置干扰原 test_usage 源码切片，移回已有事件区后通过。

## 验证结果

- 两处修复前：新增回归 2 failed、18 passed；修复后字幕及 composition 测试 21 passed。
- 最终独立代码快照全量：797 passed in 101.54s。快照包括 source、integrations、operations，仅覆盖本次功能文件，排除其他会话未完成的诊断改动和本地开发数据。
- JS 语法、git diff --check 通过。
- 更新本地 8792，PID 72166，healthz=ok、worker_alive=true。真实保存/预览 HTTP 200，字体 DejaVuSerif-Bold，字号 60，两行保留，正文 #12AB34、强调 #EF2468。
- Grok 复审结论：两项均已解决，由有条件通过改为通过，未发现残余已确认缺陷。

## 审查局限

Grok 是静态审查，没有运行浏览器或测试；测试及本地服务验证由本代理完成。复审针对两处修复和新增测试，不是重新审查所有源码。预览不同分辨率的舍入、取色器浏览器行为等在首轮列为待验证风险，不算已确认缺陷。未部署生产，未提交包含其他会话改动的混合工作区。

## Grok 完整最终报告

**有条件通过。** 常规 Hook 编辑器的字体、文字色、强调色、字号覆盖，以及预览失效和成片按 1080 基准缩放，代码链路是通的。composition 开头字幕在「按 Enter 手动换行」上不成立：保存会把换行折成空格。这条约未修之前，不能认为两种编辑器都满足本次需求。

## 已确认缺陷

### P1 此次引入（折叠语句本身既存）

`compile_composition` 在写入 `hook_text` 之前把开头字幕换行换成空格。新的统一面板却对 composition 和常规 Hook 使用同一句「按 Enter 手动换行」。

- 位置：`source/exactcut/intake.py` `compile_composition` 第 353 行 `replace('\n', ' ')`，第 391 行把该结果写入 `hook_text` 和 `composition.opening.text`。`source/exactcut/web/app.js` `hookSettings` 第 53 行的换行说明不区分编辑器；`compositionCard` 第 42 行调用它。`source/exactcut/workflow.py` `_save_draft` 第 535–538 行每次保存都会编译。
- 触发：在成片开头字幕中输入两行或三行，保存，或点「保存并预览字幕」（预览会先保存）。
- 影响：换行在草稿里消失，预览和成片都按折成一行的文案排。固定字号不再自动缩小，原本能分成三行放下的句子可能被判溢出，或变成用户没写过的单行。保存成功后文本框也会显示折行后的内容。常规 Hook 的 `hook_text` 不经过这行替换，手动换行仍保留。
- 最小修复：编译开头字幕时只去掉 `\r`，保留 `\n`，继续交给 `validate_hook_layout` / `_wrap_lines` 做最多三行校验。补一条测试：`opening.text` 为 `第一行\n第二行` 时，编译后的 `hook_text` 和 `opening.text` 仍含换行，且在合法字号下 `layout.lines` 为这两行。
- 归属：`.replace('\n', ' ')` 不在本次 diff 中，是既存行为。本次删掉了「当前成片仍自动适配字号和换行」，改成统一面板承诺手动换行，因此对用户成为新缺陷。

### P2 此次放大既有文案

显式小字号会稳定触发一条建议改文案或换行的警告，但字号已被用户锁死，改文案或换行不会把字号变大。

- 位置：`source/exactcut/hook_presentation.py` `presentation` 第 178 行。条件是 `layout.font_size / width * 360 < 16`。
- 触发：1080 宽下把字号设为 32–47（接口允许 32–96），生成预览。此时渲染字号等于设定值，`32/1080*360 ≈ 10.7 < 16`，警告必出现。
- 影响：合法小字号被提示「手机预览字号偏小，建议精简文案或调整换行」。预览和成片仍使用该字号，没有悄悄缩小。
- 最小修复：存在 `font_size_override` 时改成说明「当前为指定字号」，或只在自动字号落到该阈值时沿用原句。验收：字号 32 和 48 各预览一次，32 的提示不再要求用户靠改文案抬字号，48 不出现该警告。
- 归属：阈值和文案是既有逻辑；本次才允许用户合法选中 32–47。

## 已核对且未发现缺陷的部分

- 覆盖关系与代码一致。`apply_hook_typography`（`hook_visual.py` 321–328 行）只在字体非 `auto` 时换字体，只在颜色非空时改 `base_l1`/`base_l2` 或 `accent`，描边和次要色仍来自风格。`font_size` 为空则 `font_size_override` 为 `None`，走原自动字号。
- 切换风格不会清掉自定义。常规编辑器写 `hook_options.style`；composition 在 `compile_composition` 391–395 行用白名单保留 `font`、`font_size`、`text_color`、`accent_color`、`preview_key`。恢复默认在 `app.js` 119 行把四项设回 `auto`/`null` 并重绘。
- 放不下会报错，不会把指定字号改小。`_wrap_lines` 378–385 行失败即抛出「当前字号无法在最多三行的安全区域内完整显示」。`validate_draft` 526–528 行把字体、字号、风格传进校验。
- 旧预览会失效。`preview_identity` 含 `HOOK_LAYOUT_VERSION`（现为 9）和除 `preview_key` 外的全部 options。前端改字体、颜色、字号、风格、正文都会 `invalidateHook`。服务端身份不符则 `approved_preview` 返回空，再按当前 options 走 `presentation`。
- 成片沿用已批准决策里的字体、色板和 `font_size_override`，并按输出宽度重算。`renderer.py` `_apply_hook` 111–118 行；无批准预览时 116–118 行走 `presentation`。已读测试断言 1080 基准 48 在 540 宽输出为 24，且正文色、强调色保留。
- 克隆整份复制 `draft.json`（`workflow.py` `_clone_batch` 567–582 行），不会按旧字段表丢掉新键。缺字段时 `HookOptions` 默认 `font='auto'`、颜色和字号为 `null`。`schema_version` 仍为 1。
- 「不强调」时强调色不参与绘制。`mode=='none'` 的 highlights 为空；`render_personalized_hook_png` 500–505 行只给高亮片段用 `accent`。composition 强制 `mode='none'`，面板不提供强调色，与该行为一致。
- 字体配置与选项对应：`sans`→`default`，`sans_italic`→`neon_reversal`，`serif`→`ceo_luxury`，`cjk`→`title_card`，且 `fonts/manifest.json` 有这些 profile。

## 待验证风险（不是已确认缺陷）

- 预览接口本体这次没读到。常规编辑器自定义预览已由你方浏览器验收通过，且绘图入口是 `presentation`。若预览另有一条不调用 `apply_hook_typography` 的路径，当前结论不覆盖它。
- 已批准预览会在成片宽度上重新 `_wrap_lines`，不是把预览图放大。预览宽度与成片宽度不是同一比例时，`round(font_size * width / 1080)` 有可能改变换行。本次不能确认预览端实际宽度。
- 颜色为「随风格」时，控件仍显示 `#ffffff` 或 `#ffb347`。只有 `input` 才会把该值写入覆盖。仅打开取色器会不会触发 `input`，需要在目标浏览器上看一次。
- 指定字号时，若保留手动换行放不下、整段重排放得下，`_wrap_lines` 378–385 行会采用重排，字号不变。这与「禁止悄悄缩小」不冲突；是否还要在改换行时单独提示，需要产品确认。

你提到的全量 791 通过、3 失败：用量切片失败已按你的说明由监听器移位修复，当前 `app.js` 119 行的复位监听在 `requests` 处理区内。另外两项生产环境失败未读到失败断言，不能归到字幕字段，也不能排除。

## 需求覆盖

| 需求 | 结论 |
| --- | --- |
| 字体 / 文字色 / 强调色 / 字号，未设置则沿用风格 | 常规 Hook 与 composition 的字段保存、渲染均覆盖 |
| 切换风格保留自定义，一键恢复默认 | 已覆盖 |
| 手动换行，最多三行，放不下提示 | 常规 Hook 已覆盖；composition 保存时丢失换行，未覆盖 |
| 预览与成片一致，改设置使旧预览失效 | 常规路径已覆盖；composition 两者一致，但一致在折行之后的文案上 |
| 旧 schema 1 / 旧项目默认行为 | 已覆盖 |
| 合并为一个字幕设置面板，并解释覆盖关系 | 界面结构已覆盖；composition 的换行说明与保存结果不符 |
| 不发布、不加对白识别 | 本次 diff 未包含这些改动 |

## 审查局限

结论只基于已经读过的 `hook_options.py`、`hook_visual.py`、`hook_presentation.py`、`renderer.py`、`intake.py` 的编译与校验、`workflow.py` 的保存与克隆、`models.py`、`fonts.py` 与 manifest、`web/app.js`、`test_hook_typography.py` 和 `test_phase2_hook_workflow.py` 中的批准预览测试，外加 `changes.diff`。未再读预览路由、`plain_hook`、`ValidationError` 继承关系，也未重跑测试或打开 8792。已读测试没有锁住 composition 换行；其余未读测试里是否已有用例，不能下结论。

## Grok 修复复审原文

**两项均已解决。** 原先因此给出的「有条件通过」可以改为通过。这两处修复没有新的已确认缺陷。

## P1 开头字幕换行

`compile_composition` 现在只删除 `\r`，不再把 `\n` 换成空格。`First line\r\nSecond line` 会保存为 `First line\nSecond line`，并且 `hook_text` 与 `opening.text` 相同。随后的排版得到 `['First line', 'Second line']`。浏览器里按 Enter 产生的 `\n`，以及 `\r\n`，都会留下来。固定字号下的预览和成片都按这个文本排，不再在保存时被折成一行。

## P2 指定小字号警告

字号偏小的判断未改：1080 宽基准下，渲染字号换算到 360 宽后小于 16 才警告。指定了 `font_size` 时，文案改为「当前为指定字号，建议调大字号」，不再建议精简文案。未指定字号时仍用原句。测试与公式一致：32 出现「调大字号」且不含「精简文案」，48 的警告为空。

## 残余已确认缺陷

未发现。

## 审查局限

判断只依据你贴出的两处修改和两个新测试，没有再读源码。相关 21 项测试通过只能说明这些断言已覆盖；完整回归仍在运行，不能据此声称全量通过。仅含 `\r`、不含 `\n` 的旧式换行会被直接删掉从而粘成一行，本次测试没有覆盖；网页文本框的 Enter 不走这条路径。警告测试只断言了子串和空串，没有锁定整句文案，也没有覆盖非 1080 宽度。
