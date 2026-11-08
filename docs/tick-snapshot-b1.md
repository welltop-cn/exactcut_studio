# B1：worker_tick 内读取快照

缓存仅在 `worker_tick` 的线程本地 scope 内启用，正常退出和异常退出均清空。每次命中重新比较 inode、大小、mtime_ns 和 ctime_ns；读盘前后指纹不同则不保存。写入后通过依赖索引显式失效，其他线程可清除当前 tick 的条目，JSON 解析不占用缓存表的小锁。并发写入期间的解析结果不会重新放回缓存。

`batch_control.read` 只复用子项目身份和父项目 state；父项目指纹同时包含 registry 标记与 media-state。身份返回浅拷贝，父 state 只用于读取控制标志。control.json 始终直接读盘。`stored_jobs` 每次重新 glob，保留目录顺序，每个文件独立校验指纹，返回深度独立的副本；坏 JSON 每次仍记录异常并跳过。通用 read_json 和 material_versions.read 未接缓存。

派发前的 batch_control.read、remember_child、signal_children、change、ensure_editable、恢复中清理 control 的读改写路径，以及 reconcile 的 SIGCONT 前后均 fresh。SIGCONT 前用 fresh 读到的 control 再判断一次 paused/archived：如果用户恰好在本次扫描之后、恢复之前暂停或归档，本 tick 不再 SIGCONT（原实现会先恢复、下一 tick 再暂停）。这是本次唯一有意的行为差异，只影响这个竞态窗口。派发循环的 job 仍直接读盘；outbox 的读改写接口未改变。

batch_response 的 parent_batch_id 记忆表只存在于本次请求，成功后才保存结果，异常继续抛出，便于单独提交该函数的改动。

## 测试

- `tests/test_tick_snapshot.py`：scope 生命周期与跨 tick 不复用、线程隔离、本进程/其他线程/外部进程写入可见、同大小且恢复 mtime 的替换、错误不缓存、job 副本隔离、暂停后不派发、SIGCONT 前暂停、同 tick 完成补位，以及 20 个父项目 × 3 个内部子项目在缓存关闭/开启下三个 tick 的完整结果对照。
- `tests/test_worker_tick_behavior_lock.py`：优化前写下的调度行为基线，本次未修改，必须原样通过。
