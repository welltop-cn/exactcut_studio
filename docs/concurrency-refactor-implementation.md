# 并发改造实施记录

实施规范：`docs/concurrency-refactor-plan.md`。数据 schema 保持 1；新增任务字段可选。

## 工作边界与取舍

- 仓库开发规则实际位于 `source/AGENTS.md`，根目录没有该文件。
- 运行配置在 `source/config/default.json`、`source/config/production.json`，沿用 `util.load_config` 的合并机制。
- `config/deployment.json` 是服务器部署根内的机器配置，不在本仓库。此次不访问服务器、不修改该文件；生产 Compose 的默认额度需与部署记录中的 14 CPU / 24 GiB 一致。
- 保留任务对外的 `start` / `publish` 动作，内部以可选 `phase` / `lane` 分阶段。旧任务及 CLI 单体路径保留兼容。
- 默认各 lane 容量为 1；不同 lane 可重叠，这符合“等待不占执行位”。完全串行回退使用兼容开关；容量全设 1 是保守分 lane 配置，不能描述成全局只有一个进程。
- 共享工作注册表、协作式暂停、媒体探测缓存等按计划后移。此次复用现有正片锁、基底锁和进程身份核验机制。

## 隔离验证方法

在 `source/` 下使用 `/Users/matao/miniconda3/envs/py3.12/bin/python3.12` 运行完整 pytest。启动 pytest 前清除外部凭据及继承的 `EXACTCUT_*` 环境，使用独立临时 `EXACTCUT_DATA_ROOT`，环境为 development。

不要给整个测试进程设置固定 `EXACTCUT_SOURCE_ROOT`：已有 fixture 会切换各自的数据根，固定源根会让正片缓存跨用例污染。没有独立数据根时，production 模拟可能碰到工作区 development 标记；不要删除本地标记绕过隔离。

基线完整测试在上述隔离方式下退出码 0。新增行为用 fixtures、临时数据根和模拟外部客户端验证，不调用真实 OSS、飞书或 FBBM。

## 提交与测试证据

实施期间按三个步骤独立提交。

第 1 步：新增 lane 准入、owner 公平轮转、同 batch 串行、fetch/render 与 upload/poll 阶段推进、延迟 poll、按 lane 恢复。保留旧 CLI 与函数入口。新增回归覆盖 lane 满不阻塞其他 lane、双 render、跨 tick 公平、暂停进程、共享源缓存衔接、poll 超时和失败后人工重试。

完整测试：386 passed，57.23 秒，退出码 0（独立临时数据根）；`git diff --check` 通过。独立审查发现的重试计时、旧管理任务恢复容量和失败发布状态三处边界均已补回归。

## 回退

回退只调整运行配置或代码，保留当前数据根、任务记录及 schema 1 数据，不恢复旧数据库。切换代码前应按发布管理器现有规则等待活动执行器结束；本任务不执行发布。
