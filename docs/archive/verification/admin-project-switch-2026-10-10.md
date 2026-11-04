# Admin 项目切换性能

> 历史归档（2026-11-04）。正文保留当时的方案或执行状态，不代表当前版本；日常操作见[文档入口](../../README.md)。

用户报告 Mac mini 上 admin 切换慢，其他账号尚可。之前本机 Chrome 缓存切换计时不能代表该设备已解决。

## 证据与修复

线上 admin 有 22 个项目。旧列表响应 66,594 字节，携带 380 条事件；本机浏览器样本首包 391 ms、完整 1,313 ms。服务器只读 cProfile 测得 summaries 124 ms、1,344 次 read_json；每个项目重复扫描 61 个任务文件，全部历史事件也重复脱敏。请求耗时不能全部归因于服务端计算。

新增 `/api/batches/sidebar`，字段来自现有 renderBatches 的实际使用：id、filename、display_name、created_at、drama_id、drama_title、count、status、archived、owner。保留完整列表接口兼容性；权限在读取项目详情前过滤。精简列表一次读取任务快照，保留 production_summary 与 batch_control 的派生状态判断。列表和项目详情在锁内生成快照，释放锁后发送响应。

浏览器侧栏使用精简接口；当前项目轮询与列表、队列并行，列表慢不会阻塞当前详情显示。使用 allSettled 保持整轮结束前不启动重叠轮询。切换序号、revision、未保存草稿保护保持生效。

## 回归

新增用例先失败后通过：精简响应不携带历史、权限过滤、任务文件每轮只读一次、终态结果仍有排队任务时显示 producing、发送响应时不持全局锁、列表挂起时当前项目仍刷新。专项 38 passed；node --check 和 git diff --check 通过。

## 发布与线上验收

- 本地完整回归：847 passed in 159.26s。Linux 构建回归：845 passed（平台跳过项见版本 tests.log）。
- 2026-10-10 22:24（上海）经 root 与既有 linux-ops 发布 `us-east-20261010-admin-sidebar`，提交 `027c0c4996ed31c7a515854e86eb596f86baf50d`；镜像 `sha256:6932862d0e87e1726a81fe17713ccfc5ba589c001b3c52d62611a33d5739ce17`。没有强停制作任务。
- 公网 healthz：status=ok、release 匹配、maintenance=false、worker_alive=true。
- 发布后同一线上数据只读 cProfile：完整 summaries 125.3 ms / 1,344 次 read_json；精简 summaries 11.3 ms / 185 次 read_json，22 个项目不变。
- 本机已登录线上 admin Chrome 确认无未保存修改后刷新，脚本 `e80611592f57db5f0770`。精简列表 6,252 字节、首包 293 ms、完整 375 ms；同次对照旧接口 66,594 字节、首包 334 ms、完整 1,578 ms。响应体减少 90.6%。
- 同页真实侧栏点击：两个首次未缓存项目分别 857 / 1,239 ms 更新标题；之后缓存切回分别 4 / 3 ms，后台请求仍在途。最终恢复用户指定项目 `452471dced424bec8bd5b668e641402d`，selectedId/current.id/hash 一致。这是 DOM 计时，不是首帧保证，也不是用户 Mac mini 的测量。
- 另一个线上 producer 登录上下文读取新接口返回 200、1 个项目，所有项目 owner 均匹配本人。
- 修复在独立 worktree 的 `fix/admin-switch-performance` 分支，未覆盖原工作区其他未提交修改。Mac mini 实际体感仍需用户刷新加载新脚本后确认。
