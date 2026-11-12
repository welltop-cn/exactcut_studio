# AI 重点重试：本地 Chrome 制作验收

`${PRIVATE_TEST_ROOT}` 表示操作者的私有验收目录，仅作路径占位；这些脚本、数据和截图不随公开仓库分发。

## 运行范围

- 应用业务代码固定为 `70c7c47e6c532cd892ad559a415fade382058872`，分支 `fix/hook-validation-auto-retry`。
- 本地独立 Chrome 窗口，入口 `http://127.0.0.1:8793/home/`。独立代码和数据位于 `${PRIVATE_TEST_ROOT}/hook-validation-70c7c47/`，未使用生产素材、账号或交付回执。
- 原 18794 / 18895 测试服务保留。本次只在确认无活动任务后重启自己新建的 8793 实例，运行入口为私有 `serve_fixture.py`，release 为 `dev-hook-fixture-70c7c47`。
- 输入为本地 FFmpeg 生成的 8 秒测试图案和音频；实际输出为 360×640、约 5 秒视频。
- 私有 `adapter/sitecustomize.py` 只替换 `_request_analysis` 的模型返回，提供逐字合法、改写短语以及服务错误三种受控结果。真实的 `resolve_emphasis`、原文校验、制作 worker、阶段证据、候选晋升、FFmpeg、预览和浏览器代码均未替换。不是对真实服务商成功率的验收。
- 仅设置本地模型模拟所需的 AI 功能开关；无真实模型凭据。素材下载、发布、外部写入和 OSS 预览关闭，标题提取关闭。未创建任何交付任务。

## 从 Chrome 实际执行

所有制作均从页面的“确认内容，开始制作 →”及确认弹窗发起。单条修改测试通过“修改后重新制作”进入编辑，修改第二条 Hook，再点击“保存并校验”和确认制作。

| 场景 | 真实执行结果 | 关键证据 |
| --- | --- | --- |
| 两条素材，第二条前两次模型选词不合法 | 第一条正常成片；第二条首次两次拒绝，自动换子批次一次，随后成片 | 合法文本调用 1 次，第二条调用 3 次；候选重试额度 1；两个 current |
| 两轮均不合法 | 第一次制作的两次分析拒绝，自动再排一次；第二轮再拒绝两次后停止 | 共 4 次模型返回、2 个 outbox、最终 failed、额度 1；后续观察无新任务 |
| 服务错误 | 制作失败，未自动重制 | 1 次模型调用、1 个 outbox、额度 0 |
| 已有成功成片，只改第二条 Hook | 仅第二条 queued → processing → queued/额度1 → processing → current | 第一条没有 candidate、没有新版本或 outbox；完整版本和素材记录不变；主片/预览 SHA-256 与 mtime 均不变 |
| 最终失败文案 | 页面显示“AI 重点未通过原文校验”，明确“已自动重试一轮”，建议“修改后重新制作”改为手动选词/不强调 | Chrome accessibility tree 和失败截图，未出现“服务恢复” |
| 成功成片播放 | 两条均实际调用播放并成功解码 | duration=5.015011、readyState=4、media_error=null |

最终工作台实际显示：**“1/1 条制作完成 · 1 条沿用已有成片”**。

## 成功素材文件核对

单条修改前先保存权威状态 `single-edit-before.json`，观察脚本每 250ms 读取状态及成功文件。

保持不变的第一条当前版本为 `cc03dbb80cbb491fa33685847a4e6201`：

| 文件 | SHA-256 | 修改时间（ns，前后相同） |
| --- | --- | --- |
| 正式成片 | `65437103b3e1b74399ba7ccc5a40453d7cf8851a01baf324b24055e5d9a7a760` | `1794476724591469999` |
| 轻量预览 | `78539caece4e68006c27b5a94a62cb0ef90bc08b58c2fc709475cde001a5c88f` | `1794476724593792081` |

再次播放两条成片后，重新运行核对脚本仍通过。

## 拒绝证据

本轮累计保留 10 条独立拒绝记录，对应 10 条 `ai.emphasis_rejected` 诊断事件；每条都包含被拒短语 `invented queen`，独立文件还包含原文、模型、分析尝试序号和失败原因。最终失败及后续成功没有删除它们。

correction/progress 仍使用固定可信标记；测试没有放宽逐字匹配，也没有改写应用提示词。

## 测试夹具初次核对失败及处置

初次从页面保存夹具时，成功素材保护观察脚本报版本记录变化：夹具缺少 `brand_overlay`，前端保存补入 `{title_enabled:false, logo_enabled:false, logo:''}`。既有 `_semantic` 把全局 metadata 纳入所有素材身份，因此两条都获得了新代际。旧文件 SHA/mtime 未变，但这次检查不能算作“未重制成功素材”通过。

`material_versions.py` 的草稿语义比较和保存函数与 main 一致，本次未改它们。补齐夹具默认水印值后重跑：保存时先断言只有第二条 desired_generation 增加，再由 Chrome 确认制作，最终第一条的版本、指针、记录、文件 SHA/mtime 和 outbox 全部不变。

保留初次失败证据 `initial-brand-default-observation.json/.log`，不将夹具差异描述为应用修复或忽略失败。

## 本地证据与可复核入口

根目录：`${PRIVATE_TEST_ROOT}/hook-validation-70c7c47/`。

- `browser-acceptance-results.json` / `browser-acceptance-summary.txt`：最终断言、模型调用数、前后文件信息与完整状态转换。
- `verify_browser_evidence.py`：只读核对脚本；最终再次执行通过。
- `single-edit-before.json`、`browser-observation.json`、`rework-observation.log`：制作前基线、轮询证据与结果。
- `final-one-reused.png`、`final-validation-failure.png`：最终成功复用与失败提示截图。
- `fixture-projects.json`、`fixture_environment.py`、`seed_fixture.py`、`adapter/sitecustomize.py`：测试数据和受控模型实现。

Chrome 最后停在已完成项目的成片预览页：`http://127.0.0.1:8793/home/#5a23f54ac32d4bffa6084869bf6d5623`。服务按用户要求保留运行；未合并 main、未上线。
