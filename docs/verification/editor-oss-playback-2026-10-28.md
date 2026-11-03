# 剪切打码正式原片 OSS 播放验证

## 范围与开关

实现会话级 `GET /api/batches/{bid}/cuts/{sid}/source-url` 与 `mosaics/{sid}/source-url`。响应契约：`source` (`oss`/`local`)、`url`、`expires_in`（秒）、`reason`（`verified`/`disabled`/`receipt_unavailable`/`unavailable`）。JSON 为 `no-store`，URL 不保存到会话、草稿或诊断日志。

集中配置 `cutting.oss_playback_enabled` 默认 `false`。开启后只签已有、通过校验的正式 master `original_oss` 回执，有效期 900 秒；不上传、不 HEAD、不逐次读全文件哈希。不改预览帧率、不生成代理、不改 OSS CORS/ACL。关闭影响新的解析和重开编辑器，不能撤回已签出的 URL。

主剪切播放器、打码主播放器及“处理前”对比使用会话同 SHA 的正式成片。后两者共享源解析和回退决策，播放器意图与恢复代次分别管理。处理后、局部效果、跟踪预览、旧剪切输出、下载与准确帧路由保持原状。详细媒体清单见 [设计](../design/2026-10-28-editor-oss-playback.md)。

## 已验证的行为

- 三方 SHA、当前 master identity/大小、对象 key、bucket、endpoint 校验；回执不可用和签名异常降级本地；无权、跨项目、版本变化拒绝；签名前后校验版本；不开启时不连接 OSS。
- HTTP 真实测试覆盖两个会话入口、认证、版本 409、响应 no-store、正确回执签名。OSS SDK 使用测试替身，未触达生产。
- 浏览器状态测试覆盖解析/响应体超时、迟到响应、一次重签再一次本地、同 URL 错误重载、显式重试、过期后下次操作续签、手动单向回退、暂停/拖动/关闭取消恢复、独立播放意图、音量/静音/速率保持、原生控件竞态。
- Chromium 实际解码合成 H264/AAC 正式片字节，经模拟 OSS HTTPS Range 路由播放和定位；确认 before 与主播放器源一致、after 和下载仍为任务输出、未增加 crossorigin。
- 既有播放 Promise 测试保持；NotAllowedError 单独提示点击播放，旧尝试的 AbortError 不覆盖新状态。
- 新共享脚本走现有登录鉴权、压缩、内容哈希缓存、base-path 与安装包资源机制。

本验证的 OSS URL/签名器和网络故障均为隔离模拟；没有做生产对象下载抽样、真实用户网络或线上并发验收，不能据此承诺消除所有卡顿。准确帧 429 的独立改动见 [抽帧竞争验证](frame-contention-2026-10-28.md)。

## 测试与审查

测试命令统一使用 `~/miniconda3/envs/py3.12/bin/python3.12`，`PYTHONPATH=source`。

第一轮完整回归仅因 `test_first_load` 的固定脚本清单漏掉新增脚本而失败，已补齐，并通过新脚本的压缩、私有缓存、未登录 401 检查。

- 第二轮完整回归：`pytest source/tests -q -o addopts=''`，**1719 passed, 1 skipped, 10 warnings，413.70 秒**。警告来自既有多线程 fork 用例。日志 `/tmp/exactcut-oss-regression-final-20261028.log` 为本机临时证据。
- 审查修复后的播放器专项：`pytest source/tests/test_editor_source_ui.py source/tests/test_editor_oss_playback.py source/tests/test_editor_playback.py -q -o addopts=''`，**53 passed, 1 skipped，29.81 秒**。跳过的是旧剪切编辑器不适用的打码前后对比用例。
- 后端 source-url 与 base-path 专项全部通过；JS 语法检查与 `git diff --check` 通过。

全量回归之后的修改限于下述播放器审查问题及其测试，已重新执行播放器专项；尚未执行生产灰度。

Gemini 审查通过 Paseo Advisor，模型 `antigravity/gemini-3.8-flash-high`，agent `3f749172-367a-4774-b552-708a7f0a695b`。审查范围为实际后端、共享播放器、两编辑器接入及测试差异；没有委托修改或生产操作。

第一轮确认后端只读签名、版本/权限拒绝、所有媒体路径及默认关闭边界，提出并已处理：

1. 原生 seek 取消旧恢复后仍停在旧 src：新 seek 用最新位置和意图接续同一解析 Promise，旧代次失效，预算不重置；测试同时断言新 src 和恢复位置。
2. 初始直接本地播放误显示“已切换服务器”：仅记录实际 OSS→local 的切换，正常本地打开不提示切换。
3. 原生播放临近过期时不应中断正常视频：原生 play 只预取签名，下一次 seek 或错误再采用新地址；自定义播放按钮仍可先续签再播放。
4. 删除旧剪切编辑器未使用的 beforeBinding。

Gemini 第二轮定向复核明确通过，无遗留阻断项。顾问独立执行核心 3 例通过，实际解码/对比 5 例通过、1 例不适用跳过。此审查结论不替代现有生产发布门禁。

另外用实际编辑器事件复现并修正：load 临时时间归零时点击暂停，打码编辑器应保留源管理器目标位置，不能将 wanted 写成第 0 帧。

未发布、未打开生产配置开关、未重启共享开发/生产服务。原有 `web/app.js`、`web/index.html` 及无关文档改动保留，不纳入本次提交。
