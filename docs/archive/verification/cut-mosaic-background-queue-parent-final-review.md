# 剪切打码后台队列：父会话独立 review

> 历史归档（2026-11-04）。正文保留当时的方案或执行状态，不代表当前版本；日常操作见[文档入口](../../README.md)。

审查版本：`2c6a71d50752ae62283a91bb9d100a2dbf91b8d0`；原始基线 `5b0e4081b7c894f1f5b03213e100dd5de5347852`。
结论：本次需求范围内未发现剩余阻塞问题，开发与本地测试 review 通过。未合并 main、未发布生产。

## 独立审查与修复

检查了持久 admission/report 写序、用户调度、取消/进程租约、素材 gate、纳管 mutation、前端恢复、确认替换及维护生命周期的 diff。

首轮独立专项29通过后，额外复现两项问题并退回 D1：

1. 确认替换在取得素材 gate 前读到的报告过时，成功放弃仍可被旧请求替换。修复后在 gate 内重读 session/report，重验状态、放弃、源版本及替换状态，所有非 worker 接管路径释放 gate。
2. 新提交被拒绝后，恢复旧 cancelled 报告覆盖用户刚修改的参数。修复后确定拒绝保留编辑并显示错误；未知结果只查询且保持冻结，用 submission_id、任务ID、源版本及参数核对当前提交，冻结任务以服务端状态为准。

父会话原始两项回归修复前2失败，最终版本2通过。D1 已将对应边界纳入正式测试。

## 父会话独立验证

- 最终队列六组专项：44 passed / 56.73s，包含真实HTTP/Chromium/FFmpeg、队列恢复、worker租约、前端回执和维护。
- `tests/test_mosaic_render.py`：38 passed / 28.83s，包含新增 gate 内重验及正常/重复替换。
- `git diff --check 5b0e408..HEAD` 与 `node --check source/exactcut/web/mosaic.js` 通过。
- 核对 `/tmp/d1-review-full.log`：1943 passed、1 skipped、10既有fork警告，515.42s；这是 D1 执行的完整回归，父会话独立执行范围如上。
- 核对真实丢响应证据 JSON：POST=1、task=1，completed/84帧、等待确认、源SHA不变、JS错误0。

一次把 `/tmp/test_parent_mosaic_review.py` 与仓库专项放在同一pytest进程的运行出现失败并超时。进一步 `--setup-plan` 确认临时文件导入的 autouse `final_queue` 被应用到队列核心测试，真实dispatcher抢走测试手动claim的任务。此为父会话临时测试组装污染，不是产品修复；不把该混合命令列为通过。临时复现与正式用例分开运行通过，正式完整回归也通过。

## 验证限制

默认全站重计算串行，每用户FIFO、跨用户公平。服务器只有只读瞬时容量证据，不提高真实并发。真实SAM推理未配置，跟踪使用明确mock遮罩；FFmpeg合成、帧/像素/音频验证为真实。未做生产压力或断电/文件系统损坏验收。临时dev已由D1停止，数据及证据保留；本轮没有部署生产。
