# 整片识别失败调查与恢复

> 历史归档（2026-11-04）。正文保留当时的方案或执行状态，不代表当前版本；日常操作见[文档入口](../../README.md)。

2026-10-28，生产版本 us-east-20261027-playback-a5f7633。

项目 91f0aaa0aae54aceb28093ea8206b7d2、会话 681c7d48c762a61f56cd7ae778e5213a 的原扫描 0ee771f7a7204bccb2c7b9ed950238b0 于 09:27:02 UTC 创建，状态 failed，进度停在视频抽样识别，events 为空。非 CutError 异常仅保存通用提示，未记录异常，历史根因无法确认。

使用该实例配置查询模型，gemini-3.8-flash 可用。同源视频独立调用 detect（持有 scan/compute 锁，不写会话报告），64 秒成功返回 1 个疑似片段。随后通过正式 mosaic_scan.start 重试任务 b38dbfa9e5aa4936a2969fdcb607fa27，最终 completed，1 个片段、1 个已定位区域。未修改原成片、用户草稿，未部署或重启服务。成功重试不等于历史根因已修复。

本地补充 mosaic.scan_failed 结构化诊断，关联项目、会话、任务、模型及异常类型/调用栈；排除异常消息，避免记录供应商响应正文。页面继续显示现有安全提示。

验证：新增回归先因缺少诊断失败；补丁后 test_mosaic_scan.py、test_cutting_scan.py、test_diagnostics.py 共 51 项通过。git diff --check 通过。代码尚未发布；本次恢复由现有生产代码完成。

## 后续日志补全

按用户要求补全整片识别诊断：

- 扫描启动、复用、资源准入失败、线程启动失败、中断恢复、扫描完成/失败。
- 后台线程通过 copy_context 继承 HTTP trace_id/client_request_id，绑定 batch_id/session_id/job_id/model，并在读取会话后绑定业务 request_id。
- 代理转码、限流等待、模型调用、结果解析、抽帧记录 started/completed/failed 和 duration_ms。代理大小、视频时长及抽样帧率单独记录。
- 自动区域定位记录片段 ID、帧号、耗时、候选数量；失败保留错误元数据；跳过标明 event_limit 或 deadline。手动单帧识别也关联后台任务和请求。
- 扫描完成记录疑似片段数、定位成功数、警告数；失败记录所在阶段和总耗时。
- private_exception 不调用异常 str，不保存异常消息、供应商正文、图像、提示词或命令参数；只保留类型、文件名/行号/函数栈、cause 类型，以及有效的数字 HTTP code。APIError.code 已从本地安装的 Google SDK 源码核实。

新增生命周期/请求关联与真实 SDK 429 异常测试，先失败后通过；扩展现有视频识别、单帧拒绝测试，验证阶段日志及敏感正文排除。最终 61 passed in 19.03s，git diff --check 通过。没有真实模型调用、没有服务重启或生产发布。部署后可使用现有诊断命令按项目或任务查询：

```sh
python -m exactcut.diagnostics --root /srv/exactcut/data --batch 91f0aaa0aae54aceb28093ea8206b7d2 --hours 24 --limit 500
python -m exactcut.diagnostics --root /srv/exactcut/data --job <扫描任务ID> --hours 24 --limit 500
```

查看全过程时不要加 --errors，否则会过滤成功阶段和耗时。日志沿用现有启用开关、滚动大小与保留策略，不增加逐帧轮询日志。
