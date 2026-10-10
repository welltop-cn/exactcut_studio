# Admin 项目切换性能

用户报告 Mac mini 上 admin 切换慢，其他账号尚可。之前本机 Chrome 缓存切换计时不能代表该设备已解决。

## 证据与修复

线上 admin 有 22 个项目。旧列表响应 66,594 字节，携带 380 条事件；本机浏览器样本首包 391 ms、完整 1,313 ms。服务器只读 cProfile 测得 summaries 124 ms、1,344 次 read_json；每个项目重复扫描 61 个任务文件，全部历史事件也重复脱敏。请求耗时不能全部归因于服务端计算。

新增 `/api/batches/sidebar`，字段来自现有 renderBatches 的实际使用：id、filename、display_name、created_at、drama_id、drama_title、count、status、archived、owner。保留完整列表接口兼容性；权限在读取项目详情前过滤。精简列表一次读取任务快照，保留 production_summary 与 batch_control 的派生状态判断。列表和项目详情在锁内生成快照，释放锁后发送响应。

浏览器侧栏使用精简接口；当前项目轮询与列表、队列并行，列表慢不会阻塞当前详情显示。使用 allSettled 保持整轮结束前不启动重叠轮询。切换序号、revision、未保存草稿保护保持生效。

## 回归

新增用例先失败后通过：精简响应不携带历史、权限过滤、任务文件每轮只读一次、终态结果仍有排队任务时显示 producing、发送响应时不持全局锁、列表挂起时当前项目仍刷新。专项 38 passed；node --check 和 git diff --check 通过。
