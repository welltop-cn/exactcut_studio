# 剪切与打码播放取消竞态修复（本地，未部署）

## 目标和行为

依据 [服务器测试实例复现](playback-reproduction-2026-10-27.md)：播放尚未开始时再次拖动，会主动 pause 并使 play Promise 以 AbortError 拒绝；旧版一律显示无法播放，成功后仍残留错误文案。

两个编辑器为每次播放分配递增编号。所有显式原片暂停路径（拖动、暂停、结果切换、提交剪切、打开、关闭）先使旧请求失效。拒绝回调仍执行既有诊断上报，但只有当前编号、epoch、context 均匹配时才显示错误；仍有效的 AbortError、NotAllowedError、NotSupportedError 不会按名称被直接忽略。

播放错误和普通提示分别保存并组合显示；仍有效的播放失败不会因扫描或校验提示更新被吞掉，原片 playing 时只清除播放部分，其他提示继续保留。没有调整全局计算/抽帧锁、视频 Range、播放重试策略或服务端 409 校验。

## 验证

- 新增 tests/test_editor_playback.py：24 项 Chromium 测试，使用实际编辑器脚本及可控媒体 Promise，覆盖两个编辑器的拖动/暂停/关闭/重开、旧请求迟到、当前真实失败、恢复播放、其他消息所有权。
- 修改前运行初版 18 项：16 失败、2 通过，失败点为取消误报和恢复后文案残留；修改后新增 24 项全部通过。
- 既有 tests/test_diagnostics.py：27 项通过。
- 既有 tests/test_cutting_ui.py / test_mosaic_ui.py：22 + 69 项通过。
- 最终 142 项相关测试全部通过（含 24 项新增用例）；两个 JS 文件语法检查及 git diff --check 通过。
- 未跑全仓库发布回归，未在服务器部署本次修复；正式实例和当前测试实例代码均未改动。

## 独立 review

用户指定 Pi + DeepSeek Flash high。调用已安装 Pi，使用已验证兼容的 Node 22.19.0，仅允许 read/grep/find/ls，关闭扩展和 MCP。模型为 deepseek/deepseek-flash，thinking=high。审查对象为 2533fec 基线后的本地修改与新增测试。

首轮 Pi 审查要求修复结果页完成路径遗漏暂停、状态版本判断吞有效错误。已移除 statusRevision 判定，分别存储两类提示；showResult(true) 无条件暂停，paintBatch 首次显示结果页时再次暂停；open 先清除旧 context 后暂停，onpause 忽略与当前 paused 状态不符的迟到事件。新增生成开始/完成、剪切结果及关闭重开迟到 pause 事件覆盖。首轮报告中的 18/20 项差异来自 reviewer 读取后补入的两个测试；以实际 collect-only 为准。

[首轮 review 原文](playback-pi-review-2026-10-27.md)提出修改意见；修正后 [Pi + DeepSeek Flash high 复审](playback-pi-rereview-2026-10-27.md)结论为“通过”，未发现新的确定性阻塞项或高风险回归。两轮均为静态只读审查，测试由主执行者运行。用例数量以 pytest 收集为准（review 原文中的分项算式有笔误，总数 24 正确）。当前仅完成本地修复及相关回归，未部署。
