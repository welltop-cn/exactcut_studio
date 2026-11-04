> 历史归档（2026-11-04）。正文保留当时的方案或执行状态，不代表当前版本；日常操作见[文档入口](../../README.md)。

**结论：通过。** 6 项修改在代码与测试中均已落实且自洽，未发现阻塞性缺陷或新引入问题。

**非阻塞运维条件：**

1. `runtime.json` 的 `concurrency.hook_previews` 需显式设为 3（模板改动不生效）；发布后核对启动日志 `Effective concurrency settings`，否则预览上限仍为 1。

2. 新增 `logging.browser_events_per_minute` 默认 240；若在 runtime 覆盖必须为正整数，否则 `configure` 启动即抛错。该上限按账号计，单账号每分钟仍可写最多 240 条 `browser.event` + 1 条 `browser.rate_limited`。限流键仍含 `client_request_id/route/status`，同一问题在一分钟内每次请求仍各写一行（已被上限约束）。若想进一步降噪，可把客户端可控字段移出键，属可选加固。

3. 全局 `_throttled` 超 4096 整体 `clear()` 的既有行为未变，多账号高频可间接重置其它事件去重，建议纳入日志量观察。

4. `release_hook_preview` 仅捕获 `OSError/ValueError/RuntimeError`；当前 `storage.release`/`reservation_lock` 路径的实际异常均在此范围，若未来引入其他异常类型需同步扩围。

以上均不影响本次发布判定。
