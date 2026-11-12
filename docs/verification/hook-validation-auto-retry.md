# AI 重点校验失败自动重试验收

## 范围

基于 `main` 的 `fd2e5084e0c7e62242a9faae277aba3812309570`，独立分支 `fix/hook-validation-auto-retry`。不依赖告警链接修复分支。仅本地隔离验证，未合并、未部署、未调用真实 AI 或上传生产素材。

## 行为

- 原有两次模型分析仍严格要求重点逐字、连续匹配原文。最终校验拒绝使用独立 `HookEmphasisValidationError` 类型；服务调用失败、资源等待及其他错误不属于此类型。
- 纳管素材失败后，通过父项目权威记录原子替换该候选的内部子批次，继续使用原确认快照和单条素材清单，复用现有 outbox / fetch / render 流程。候选记录持久化 `hook_validation_retries=1`；新进程或重复恢复不能获得第二次自动重试额度。
- 普通项目单条 render 任务使用现有阶段重排逻辑，额度记录于该任务。不会重排整批。
- 再次失败后停止，并明确提示系统已经自动重试，使用“修改后重新制作”将该条 Hook 改成手动选词或不强调。首次校验拒绝说明系统会自动重试一轮，不再套用“服务恢复”。
- 每次校验拒绝独立写入 `projects/<剧号>/cache/hook-emphasis/rejections/<随机编号>.json`。记录包含输入原文、模型、分析尝试序号、被拒 highlights、原因、时间、提示词指纹及已有诊断关联信息；文件权限为 0600。后续失败、恢复或成功均不删除这份证据。
- 诊断事件 `ai.emphasis_rejected` 记录 `analysis_attempt`、有界 `rejected_phrases`、原因及独立记录路径。`production.hook_validation_retry` 关联原/新子批次和素材。
- 拒绝记录不读取为模型指令；correction / progress 仍只使用既有可信固定标记。

## 成功素材保护

自动重排只更新失败候选的 `child_id`、状态、重试额度和对应 outbox，沿用原候选的单条 manifest。它不改成功素材的版本、历史、成片及预览，也不创建交付任务。

回归逐项验证：

1. 同项目另一条已成功素材的完整版本记录与当前版本指针保持一致。
2. 自动重试成功及再次失败两条路径，成功素材正式成片和预览的 SHA-256、修改时间保持不变。
3. 失败素材自身有旧成功版本时，原版本、指针、历史及成片 SHA-256、修改时间保持不变。
4. 旧执行器迟到成功、重复失败回调、多次 outbox 恢复均不能提交旧结果或无限重排。
5. 暂停、归档、删除、草稿代际改变、执行身份变化和已经成功的结果均阻止自动重排。
6. 替换子批次后，恢复流程只物化候选当前绑定的子批次，不重新唤起旧 outbox。

## 测试

修复前分别复现：拒绝记录数量为 0；校验失败后候选状态直接 failed，没有重排；普通项目单条任务同样不重排；替换后旧 outbox 被错误恢复为 materialized。对应测试修复后通过。

最终相关测试命令：

```sh
cd source
/Users/matao/miniconda3/envs/py3.12/bin/python3.12 -m pytest \
  tests/test_material_versions.py tests/test_intra_project_results.py \
  tests/test_hook_emphasis.py tests/test_step3_resources.py \
  tests/test_public_errors.py tests/test_concurrency_recovery.py \
  tests/test_managed_scheduler_verification.py tests/test_managed_verification_retry.py -m ''
```

结果：**308 通过**，包含相关 integration 层。使用隔离素材夹具和模拟模型；不能据此承诺真实模型下一轮必定成功。

最终代码的全仓库默认层：`/Users/matao/miniconda3/envs/py3.12/bin/python3.12 -m pytest`，**1657 通过、1 跳过、1347 未选择，150.27 秒**。未选择项为默认排除的 UI / integration 层；相关八文件已另用 `-m ''` 执行全部层，不将默认单元层结果描述为全仓所有层。

AST 与 main 比对：`SYSTEM`、`grounded_analysis`、`_request_analysis`、`Analysis`、`Highlight`、`manual_emphasis`、`resolve_emphasis` 完全一致，原提示词、校验、手动路径及成功缓存路径没有改变。

后续已在独立 Chrome 中实际点击制作、运行真实 worker / FFmpeg 并播放成片，覆盖自动重试、停止条件与成功素材文件保护，见 [本地 Chrome 验收](hook-validation-dev-chrome.md)。模型返回受控模拟，未调用真实服务商或对外交付。

差异复核见 [AI 重点重试差异审查](hook-validation-differential-review.md)。
