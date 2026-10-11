# 项目只读与轮询修复发布

- 用户批准后于 2026-10-11T02:53:15Z 发布 us-east-20261011-ui-8097b71。
- 业务提交 8097b713a719a7c0929c708cf14cd7d3dcf30d82，包含 94e67b8 的新建项目保存校验修复。
- 镜像 sha256:f98eeb9486794e5a775b30657d619d88ad414db1ba35575c83c14be7cff2a165，与运行容器一致。
- 本地隔离全套 1018 项通过；Linux 1012 passed、6 skipped，193.03 秒。发布前 active_tasks 和 phase_blockers 为空，allowed=true。
- 发布后 healthz=ok，worker_alive=true，maintenance=false。线上封存 app.js SHA256 与本地一致。
- 真实浏览器使用既有 hookqa212220a，原项目 39b4f695712a4c44ba9a0af940e821d2：review 下直接打开标准化内容，字段禁用、editorToolbar 隐藏、正常 sourcePrefetch 提示隐藏。点击修改后重新制作，保持同一项目，editor 模式、字段启用、工具栏可见、dirty=false。再次刷新回到只读。
- 此次未保存草稿或启动制作，未提交 FBBM。此前 source-status-polling-local 报告的未发布状态由本记录更新。
- 真实可见标签页 Resource Timing 记录 sidebar 请求起点 18511、33510、48510 毫秒，间隔约 15 秒；正常正片提示持续隐藏。浏览器任务已释放。
