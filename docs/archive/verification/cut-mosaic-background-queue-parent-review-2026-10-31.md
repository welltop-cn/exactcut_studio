# 剪切打码后台队列：父会话 review 修复记录

> 历史归档（2026-11-04）。正文保留当时的方案或执行状态，不代表当前版本；日常操作见[文档入口](../../README.md)。

日期：2026-10-31。修复基线：`bc37c09d5465fb99771bd9885920e074c4386aca`。分支仍为 `feat/cut-mosaic-background-queue`。

代码修复提交：`bf99db923dac6b3d1e7a34fa8228e91dd89545cf`（锁内确认准入）与 `e9b03a30f2d59c4db69d332c42c5e9dedd1e0132`（拒绝/未知回执恢复与关联）。

父会话首轮 review 未通过；原来的 29 项专项未覆盖下面两种交错。D1 按 `/tmp/test_parent_mosaic_review.py` 原样复跑，得到 **2 failed / 4.09 秒**，随后转成仓库内正式回归。本轮不部署、不合并 main，修复提交后继续由父会话独立复审。

## 1. 成功放弃后仍能被旧确认覆盖

原实现先调用 `mosaic_render.state` 读取 completed 报告，再取得素材 gate。另一个窗口可在这两步之间完成 `discard_result`，原确认请求仍用旧的未放弃报告启动替换。

修复：`mosaic_replace.start` 保留锁外查询用于 ID 校验与旧替换恢复，但所有准入决定都在素材 gate 内重新读取 session 与 render 报告后作出。重新核对 reset、discarded_at、completed 状态、preview、源 SHA、replacement 状态和当前媒体版本。已经替换成当前版本的请求仍幂等返回 completed；异常和幂等返回均通过 finally 释放 gate，只有真实启动的替换 worker 接管 gate/lease。

正式测试：

- `test_confirmation_rechecks_discard_committed_before_asset_gate[False/True]`：legacy / managed 两条真实 FFmpeg 结果，在确认取得 gate 前插入真实 `discard_result` 并确认已落盘；确认必须抛“放弃”，不得启动替换线程，报告不进入 replacement.running，当前源 SHA 不变。
- `test_confirmation_revalidates_report_and_version_after_gate`：可靠交错分别改变任务状态、报告源 SHA、replacement.running、replacement.completed、当前成片版本；不得用锁外旧报告启动线程，并验证 gate 已释放。
- 原有 legacy / managed 真正替换、重复确认、再次打开新版本的回归继续保留。

## 2. 提交被拒绝后旧终态报告覆盖新编辑

原实现对所有 POST 异常都进入 GET 恢复，并无条件 `restoreParameters`。父会话的例子是旧 cancelled 有两段删除、用户删掉一段，新提交因维护被拒绝，后续 GET 却把两段旧选择写回了编辑器，且错误消失。

修复：

- 确定拒绝（普通应用错误、409、维护 503 等）直接保留当前参数和本机草稿，显示“提交失败”及原错误，不用旧终态记录覆盖；方案摘要与错误同时显示。
- 不确定结果（outcomeUnknown、网络 TypeError、超时，以及未确定的服务器错误）继续保持锁定，只做 GET 查询，绝不自动重发 POST。
- 新增可选 `submission_id`：客户端每次点击提交只生成一次 32 位十六进制标识；服务端校验并把它存入队列 admission 快照与 render 报告。该字段只关联回执，不是用户身份/权限，也不参与方案幂等指纹；旧客户端可省略，仍使用 schema_version=1。
- 将 GET 终态任务认作本次已接受提交，需要同时匹配 submission_id、相对提交前的新任务 ID、源 SHA/reset 和规范化的 targets/ranges/effect。仅“任务 ID 变了”或“方案恰好相同”均不够。
- 服务器上的 queued/running/cancelling/完成待确认任务具有冻结权威，可以恢复其参数；旧 cancelled/failed/discarded 不能证明新提交已接受，继续查询而不覆写草稿或解锁。确认属于本次提交的终态结果不通过恢复旧参数来改变当前选择。

正式测试：

| 用例 | 断言 |
|---|---|
| cancelled / failed / discarded × 普通拒绝 / 409 / 503（9 项） | 删除 1 段保持不变；LocalStorage 仍只有 1 段；错误明确可见；可继续编辑；POST 恰好一次 |
| 丢失回执，GET 先返回旧方案 | 保持当前选择和锁定，只查询，不重提；新 queued 回执到来后正确恢复 |
| GET 返回同旧 ID 但与本次相同方案 | 不能误认本次新提交，也不能提前解锁 |
| GET 返回新 ID 但错误方案 | 不能覆写当前选择或提前解锁 |
| GET 返回不同 submission_id 但相同方案 | 不能把另一次尝试当成本次回执；该反例先红灯再修复 |
| TypeError 丢失回执，但本次已 completed | 恢复结果预览和确认；返回查看参数后仍保持待确认冻结；POST 恰好一次 |
| 实际 HTTP submission_id 校验与持久化 | 错误格式 400；有效标识在响应、GET 报告和 queue 快照一致；更换关联标识重发同方案仍返回原任务 ID，不重复入队 |

UI 故障分支使用真实 Chromium 与确定性的 API 故障注入；receipt 持久化另通过实际 HTTP 和真实存储验证。原有真实 HTTP+Chromium+FFmpeg 完整流程继续在本轮回归中执行。

此外执行了实际 HTTP 响应丢失验收：Playwright `route.fetch()` 先把 POST 转发给真实服务，核实 202、queue 快照及 submission_id 已落盘，再 `route.abort()` 丢弃回执。页面保持冻结，仅 GET 恢复，最终 POST=1、task=1、真实 FFmpeg 输出84帧并待确认，原片SHA不变、JS错误0。公开[丢回执证据](cut-mosaic-background-queue-lost-response-2026-10-31.json)已提交；私有可重放脚本为 `source/.dev-state/d1-review-lost-response/acceptance.py`，数据在同目录。临时服务 `127.0.0.1:63919` 已 finally shutdown/server_close，核验无监听。日志中有浏览器切换结果视图时中止 frame 请求产生的 BrokenPipe；它发生在图片响应写出，未影响任务结果，不将其包装为“服务日志零错误”。

## 验证结果

- 父会话原始复现：修复前 2 failed。
- 正式基础回归红灯：12 failed（放弃交错 2、确定拒绝 9、未知回执恢复 1）。
- 更严格的 receipt 身份反例与 HTTP 校验：修复前 2 failed。
- 修复后父原始复现 + 正式基础回归：14 passed / 27.79 秒。
- 相关整体第一轮：136 passed / 1 skipped / 118.68 秒。
- receipt 完整修复后父原始复现 + UI/HTTP 关联专项：20 passed / 41.74 秒。
- 锁内状态/版本与正常、重复确认：9 passed / 11.10 秒。
- 最终代码提交后原样复跑父会话临时文件：2 passed / 3.70 秒；JS语法、目标Python编译和diff检查通过。
- 本次最终完整回归：**1943 passed / 1 skipped / 10 warnings，0失败，515.42秒**。唯一 skipped 仍是 cut 分支不适用 mosaic 前后对比；10个警告仍是既有 multiprocessing/fork DeprecationWarning。

完整回归命令：

```sh
cd source
~/miniconda3/envs/py3.12/bin/python3.12 -m pytest -o addopts='' -q
```

父会话原始复现仍可直接复跑；仓库内正式测试不依赖该临时文件：

```sh
cd source
PYTHONPATH=.:tests ~/miniconda3/envs/py3.12/bin/python3.12 -m pytest -o addopts='' /tmp/test_parent_mosaic_review.py -q
~/miniconda3/envs/py3.12/bin/python3.12 -m pytest -o addopts='' tests/test_mosaic_render.py tests/test_mosaic_queue_ui.py tests/test_mosaic_queue_integration.py -q
```

本轮只使用测试临时/私有数据根、合成素材与测试账号，未重启 18883 或其他常驻 dev，未调用付费识别/生产数据/发布接口。SAM 和容量调查的边界仍按[首轮验证记录](cut-mosaic-background-queue-2026-10-31.md)执行。
