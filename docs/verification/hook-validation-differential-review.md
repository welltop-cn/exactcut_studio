# AI 重点自动重试差异审查

## 结论

基线：`main@fd2e5084e0c7e62242a9faae277aba3812309570`。审查独立分支 `fix/hook-validation-auto-retry` 的四个业务文件、四个测试文件和验收文档。

最终未发现尚未处理的严重、高危或中危缺陷。总体变更风险为中等：涉及制作状态恢复，需以隔离回归和人工代码审阅作为合并依据；本结论不授权合并或上线。

| 严重性 | 尚未解决数量 |
| --- | ---: |
| 严重 / 高 / 中 / 低 | 0 / 0 / 0 / 0 |

## 变更与影响范围

| 文件 / 函数 | 变更 | 影响评估 |
| --- | --- | --- |
| `hook_emphasis.py:53,203` | 独立校验异常类型、拒绝证据和日志 | 中；不改源文规则、模型提示和成功路径 |
| `material_workers.py:307` | 有界重排失败候选 | 中；两个调用点，仍需当前身份、当前代际与父控制允许 |
| `material_workers.py:161` | 恢复只物化当前子批次 | 中；修复替换后旧 outbox 被再次恢复的路径 |
| `material_workers.py:343,402` | 完成与恢复时识别专用失败 | 中；失败之外的晋升、封存及交付分支保持原逻辑 |
| `webserver.py:481,538` | 普通项目单条 render 重排一次 | 中；finish_job 有 14 个静态调用点，新增逻辑只在已验证结果、非内部子批次、单条 render 校验失败时执行 |
| `error_guidance.py:81` | 独立失败提示 | 低；真实服务故障继续使用原分类 |

通过 Python AST 对生产代码统计：`analyze_grounded` 有 1 个直接调用点，`stage_finish` 有 1 个直接调用点，`retry_hook_validation` 有 2 个直接调用点。`finish_job` 的其他分支没有增加自动上传、重复交付或绕过核实的入口。

## 基线与历史依据

- `git blame`：原候选身份校验、成功封存和 current 晋升来自 `5e8cefe7`。这些保护全部保留，自动重排复用 `_actual`、`_eligible` 和父项目锁；不修改成功封存流程。
- 普通项目的单条 render 失败收尾来自 `fa027742`。仅在专用校验失败时增加一次排队，其他失败保留原收尾。
- outbox 恢复相关路径由 `df208799` 组织到 `_recover_parent`。本次回归证实缺少 child_id 检查时，旧记录会被再次标为 materialized；新增当前 child_id 检查后通过。
- 没有删除鉴权、OSS 回执验证、媒体哈希验证、暂停/代际/执行身份保护；持久 schema_version 保持 1，重试次数为可选元数据。

## 失败与攻击场景核对

1. 模型把恶意内容放进 phrase 或 reason：原文校验继续拒绝，拒绝证据只写独立文件及有界诊断；correction / progress 仍只存可信固定标记，模型输出不变成下一轮指令。
2. 旧执行器重复或迟到报成功：父候选已经绑定新 child，`_eligible` 拒绝旧结果。回归同时核对原成功版本和文件 SHA / mtime。
3. 重启或重复恢复消耗无限重试：次数与新 child 在同一父状态写入，二次执行仍读取次数 1；旧 outbox 不再物化。回归覆盖重复完成及恢复。
4. 暂停、归档、删除或草稿改变时仍重排：现有父控制和代际检查保持，新增回归逐一验证；进程 token 被替换同样拒绝。
5. 服务故障或资源等待被误当作模型拒绝：仅接受 `status=failed` 且 `error_type=HookEmphasisValidationError`；TransientHookAIError、普通 ValidationError、ResourceDeferred 和 OSError 均不能触发自动重制。
6. 改动误伤成功素材或交付：重排清单只有失败 asset，成功兄弟素材的完整版本、指针、主片及预览文件 SHA / mtime 保持一致；原有交付待核实回归一并执行。

## 测试与覆盖边界

最终八个相关测试文件执行所有层：**308 通过**。最终代码的全仓默认单元层：**1657 通过、1 跳过、1347 未选择**（默认排除的 UI / integration）。包括真实 resolver 的拒绝记录、真实诊断 JSONL、orchestrator 的异常类型落盘、完成/恢复重排、有界次数、成功素材保护、普通项目及交付核实。

AST 比对确认 `SYSTEM`、`grounded_analysis`、`_request_analysis`、`Analysis`、`Highlight`、`manual_emphasis`、`resolve_emphasis` 与 main 一致。

后续已增加[独立 Chrome dev 验收](hook-validation-dev-chrome.md)：真实页面点击、制作 worker、FFmpeg、成片播放、实际失败提示，以及成功兄弟素材版本/文件 SHA/mtime 的前后核对均通过。模型返回使用本地受控适配器，未使用真实 AI 模型、生产数据或外部上传；测试不保证真实模型重试一定成功。未进行生产部署或真实网络故障注入。

## 方法与建议

采用 differential-review 的差异、历史、调用点和测试覆盖检查，集中审查状态变更与成功媒体边界；没有委派独立审查者，不将自审描述成独立复审。

建议按两个独立分支分别审阅，合并/上线前仍需明确授权。本次无遗留阻断问题，验收入口见 [详细测试记录](hook-validation-auto-retry.md)。
