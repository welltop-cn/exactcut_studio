# 历史文档归档

2026-11-04 将混在日常说明中的旧计划、审查、交接、部署快照和一次性验收集中到这里。正文保留原来的成功、失败和未完成状态，仅调整链接并增加归档标识。

**日常操作请从[当前文档目录](../README.md)进入。** 归档不表示需求已经实现；当前待办和 main 合并结果见[项目状态](../status.md)。旧文中的“当前”“待发布”“未合 main”均对应记录当时，不能作为实时状态。

## 最近并发工作

- [八项并发改动的原交接](handoff/2026-11-04-concurrency-8.md)：范围、原始问题与后续性能专项。
- [接手记录](handoff/2026-11-04-concurrency-8-takeover.md)：复现与修复过程。
- [两机最终业务验收](handoff/2026-11-04-concurrency-8-final-acceptance.md)：三项核心修复、真实业务与受控场景边界。
- [main 合并前审查](handoff/2026-11-04-main-merge-review.md)：两机全量原始结果、测试修正和合并条件；随后已在 `9002646` 合入并推送 main。
- [长片合成范围外问题](verification/concurrency-followups-awaiting-approval.md)：A 长片仍失败，用户决定暂记，未实施拟议优化。

## 按资料类型查找

| 位置 | 内容 |
| --- | --- |
| [verification/](verification/) | 一次性功能、发布、审查与真实/模拟验收记录及证据附件 |
| [handoff/](handoff/) | 固定日期与版本的交接 |
| [design/](design/) | 设计讨论、研究、接口契约和待验收方案 |
| [superpowers/plans/](superpowers/plans/) | 分阶段实施计划 |
| [superpowers/specs/](superpowers/specs/) | 专题设计稿 |
| [source/docs/](source/docs/) | 早期 Mac 运维、暂停/归档、账号隔离和兼容 Excel 说明 |
| [initial-handoff/](initial-handoff/) | 最初交接包的根目录说明 |

本目录其他文件保存早期 Linux 迁移、旧架构、并发设计及历史部署证据。文件名未带日期的设计稿也可能已被替代，应结合提交和对应实现核对。

## 本次重写前的说明快照

- [原 source README](source/README-before-cleanup-2026-11-04.md)
- [本地服务流水记录](local-dev-services-before-cleanup-2026-11-04.md)
- [服务器测试实例流水记录](server-test-deployment-before-cleanup-2026-11-04.md)
- [Linux 部署与迁移旧说明](linux-migration-before-cleanup-2026-11-04.md)
- [诊断日志旧说明](production-diagnostics-before-cleanup-2026-11-04.md)
- [10 月 28 日用户手册与截图](素材工作台操作说明_2026-10-28.md)
- [历史 Mac 架构](现行架构与处理流程.md)

旧文代码块中的路径、端口、PID、命令及配置代表当时环境，不应直接照抄执行；有些附件只保存在当时的私有测试目录。归档未新增运行环境、部署或测试结果。

新的一次性验收记录仍可存入这里；生产变更同时更新[线上变更索引](../production-change-log.md)，未完成事项同时更新[项目状态](../status.md)。
