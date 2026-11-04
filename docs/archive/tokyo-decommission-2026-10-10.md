# 东京 Exact Cut 实例下线记录

> 历史归档（2026-11-04）。正文保留当时的方案或执行状态，不代表当前版本；日常操作见[文档入口](../README.md)。

2026-10-10，按用户要求下线 `124.156.228.202` 上的 Exact Cut。只停止本应用，不停止共享机、nginx 或其他应用，不迁移或删除数据。

## 执行结果

- 原版本 `linux-20261009-3`，容器 `exactcut-production-web-1`，数据根 `/srv/exactcut/data`。下线前任务记录为 2 个 completed，没有 queued/running；两个项目分别为 complete、draft，没有执行器运行。
- 获取既有 `deployment.lock`，写入本应用 maintenance 标记，并获取 `mutation.lock` 等待现有写请求结束，再次确认没有活动任务。
- 备份 nginx 独立片段，将当时配置的工作台前缀及其下的所有路径改为 HTTP 410。原 server/include 保留，其他路径不改。`nginx -t` 通过后 reload。
- 仅对本应用执行 Docker restart policy 更新为 `no`，然后正常停止容器。容器保留为 exited；不执行 compose down、镜像/卷清理或删除业务文件。
- 保留维护标记，当前版本记录未改变。后续启动/恢复需明确处理下线入口和维护状态，不能只凭 current.json 推断服务仍在运行。

服务器备份：`/srv/exactcut/backups/offline-tokyo-20261010T031331Z/exactcut-production.conf`。

服务器操作回执：`/srv/exactcut/history/offline-tokyo-20261010T031331Z.json`，包含原 restart policy、版本、其他容器身份和完成状态。备份目录私有，不进入 Git。

## 验证

- 公网历史工作台入口和其 healthz 均返回 410。
- 容器 `Running=false`、`Status=exited`、restart policy 为 `no`；后端 18792 无监听。
- nginx 为 active；原 PayPal、domain-watch 公网路径均仍返回 200。
- 原其他 5 个容器仍运行且身份未改变，MySQL、Postgres、Redis 健康状态正常。
- 原 2 个项目目录、账号库、配置和数据根均保留；没有删除视频或缓存。
- 新机 `8.221.122.75` 当时配置的工作台 healthz（具体前缀由部署方维护） 返回 ok、production、worker_alive=true、maintenance=false。本次未操作新机部署。

## 如需重新启用

仅在后续明确要求重新启用东京实例时操作：先核对上述回执和原版本，通过已有部署管理器恢复后台并验证健康、处理维护标记及原 restart policy；后台正常后恢复备份的 nginx 片段，执行 nginx 语法检查和 reload，再验证公网入口。恢复不还原旧账号库或覆盖业务数据。本轮未执行这些恢复操作。
