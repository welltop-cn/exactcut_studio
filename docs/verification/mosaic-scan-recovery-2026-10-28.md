# 整片识别失败调查与恢复

2026-10-28，生产版本 us-east-20261027-playback-a5f7633。

项目 91f0aaa0aae54aceb28093ea8206b7d2、会话 681c7d48c762a61f56cd7ae778e5213a 的原扫描 0ee771f7a7204bccb2c7b9ed950238b0 于 09:27:02 UTC 创建，状态 failed，进度停在视频抽样识别，events 为空。非 CutError 异常仅保存通用提示，未记录异常，历史根因无法确认。

使用该实例配置查询模型，gemini-3.8-flash 可用。同源视频独立调用 detect（持有 scan/compute 锁，不写会话报告），64 秒成功返回 1 个疑似片段。随后通过正式 mosaic_scan.start 重试任务 b38dbfa9e5aa4936a2969fdcb607fa27，最终 completed，1 个片段、1 个已定位区域。未修改原成片、用户草稿，未部署或重启服务。成功重试不等于历史根因已修复。

本地补充 mosaic.scan_failed 结构化诊断，关联项目、会话、任务、模型及异常类型/调用栈；排除异常消息，避免记录供应商响应正文。页面继续显示现有安全提示。

验证：新增回归先因缺少诊断失败；补丁后 test_mosaic_scan.py、test_cutting_scan.py、test_diagnostics.py 共 51 项通过。git diff --check 通过。代码尚未发布；本次恢复由现有生产代码完成。
