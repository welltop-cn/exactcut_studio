# 截图、浏览器与后台诊断关联验收

## 问题与变更

producer03 的字幕预览429可从线上日志证实，但需要手工对齐一分钟调度快照。检查进一步发现旧浏览器脚本用过时按钮ID、累计20条后永久停止上报，同用户同类事件10秒共用去重键，报错截图未携带HTTP请求编号。

本轮沿用JSONL、集中配置和现有trace上下文，补充：

- 服务端错误响应的trace_id与error_info.detail诊断编号；通过既有错误弹窗展示，便于截图关联。登录跳转等前端特殊处理路径和本地校验提示不保证展示该编号。
- 同源API的X-Client-Request-ID与浏览器请求前后日志；保留Request、原headers、body、signal等fetch语义，不读取请求/响应正文。
- 实际按钮和dataset动作、页面实例ID、实际app.js内容哈希、当前项目/素材行号；视频media_error/readyState/networkState/播放时间和可见性。不记录媒体src或错误正文。
- 每分钟分别40条常规、40条错误预算；细分服务端去重键；成功上报不再额外记录自身HTTP started/completed。
- 字幕预览请求revision/状态、准入具体原因及占用、各计算阶段耗时、帧数量、原片完整性、计算结束后的结果拒绝原因、资源释放；调度快照增加独立预览占用。
- 查询支持actor/event/client/page；trace匹配browser.http_trace_id，batch/request等查询可匹配浏览器元数据。

## 测试

先补回归，观察旧代码在截图trace、点击去重/上下文、actor查询和准入原因方面失败，再实现。HTTP/Node测试覆盖真实按钮、Request头与body保持、跨域不采集、分钟额度自动恢复、媒体错误、不输出敏感内容、NaN及错误类型拒绝；真实纯排版预览验证各阶段事件且不含文案/图片。

- 专项诊断：18 passed in 3.84s。
- 开发工作区全量：1115 passed、2 failed（188.31s）。失败位于test_external_flags和test_production_helper，测试切到production却访问工作区已有development目录；保留原有环境，不修改业务环境标记。
- 独立验收目录：`/tmp/exactcut-diagnostics-acceptance-20261012`，基于76c8759，仅应用本轮诊断源码和测试补丁，不包含工作区另一项静态片尾改动。先验证原失败用例所在test_external_flags基线4项通过。
- 独立候选全量：**1107 passed in 181.10s**。命令：`/Users/matao/miniconda3/envs/py3.12/bin/python3.12 -m pytest -o addopts='' -q --tb=short`，在候选source目录执行。

工作区1117项与独立候选1107项的数量不同，因为前者还包含另一项未提交功能的测试。未将其改动纳入本次补丁。

## Review与交付状态

agy尝试直接及使用现有本机代理调用，都在Google账号/资格校验阶段返回EOF，未产生review结果。用户随后指定Pi；使用已列出的 `deepseek/deepseek-flash`、`--thinking high`、只读read/grep/find/ls工具审查并发修复和诊断变更。[初轮原文](pi-deepseek-review-2026-10-12.md)结论为未发现阻塞缺陷，建议处理以下事项，已按代码与复现逐项核实：

1. 独立于客户端标识的服务器账号级硬上限：默认240条/分钟，集中logging配置可覆盖；超过时只记一次rate_limited且成功回执，防止诊断HTTP错误反而放大日志。新增绕过尝试、窗口恢复、账号独立回归。
2. 补回确认弹窗startNow动作；补浏览器Request真实重建后method/body内容、显式init的signal/body保持断言。
3. compute内部捕获的延期在当地记录phase=compute，避免仅有外层admission分支；新增真实HTTP延期观测回归。
4. 新日志参数用get读取可选字典信息，错误响应缺detail可回退，防止诊断字段读取影响业务；新增缺detail响应测试。
5. 预览释放预约发生IO错误时记录cleanup_failed；内存名额释放，保持响应可返回，陈旧账本由下一轮worker恢复。HTTP成功/失败/延期验证账本清空，释放异常验证响应仍可返回且有日志。
6. 原有round1集成fixture中INLINE_RENDERS从dict纠正为set。

review后专项：34 passed in 12.09s（diagnostics、preview capacity、round3 preview）。最终独立候选全量：**1114 passed in 182.67s**，同上Python与pytest命令。`git diff --check`通过。

[Pi增量复查原文](pi-deepseek-rereview-2026-10-12.md)：**通过**；6项修改已落实且自洽，未发现阻塞缺陷或新引入问题。保留运行配置预览上限3、服务器日志预算、既有去重表容量与异常类型范围作为上线观察项。

未改线上配置、未发布。应用日志不能覆盖完全离线、进程被OOM/SIGKILL或反向代理未转发的情况，这些仍需容器、内核和nginx日志佐证。日志容量、轮转、脱敏与写入失败不影响业务的约束沿用原实现。
