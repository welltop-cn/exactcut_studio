# 字幕设置及诊断改动生产发布

> 历史归档（2026-11-04）。正文保留当时的方案或执行状态，不代表当前版本；日常操作见[文档入口](../../README.md)。

- 目标：8.221.122.75，入口 http://8.221.122.75/home/。
- 发布时间：2026-10-10 09:40 UTC（北京时间 17:40）。
- 发布版本：`us-east-20261010-typography-diagnostics`。
- 工程提交：`d3722842035feee5df5574f2fcf5ef9b0d8032a2`，发布前工作区干净，包含字幕设置、诊断日志及前端修复的全部最新已提交改动。
- 固定镜像：`sha256:34dd0268b25ebf7557213563d851cb43d1cf8dcb8f8f7a08da5ffcbca580550a`。
- 前一版本：`us-east-20261010-home2`，保留可回滚。schema 1，生产数据及现有配置不迁移、不还原。

## 发布前验证

发布管理器对完整工程构建，隔离 Linux 测试容器未挂载生产密钥或数据，外部调用关闭：**807 passed, 2 skipped in 134.60s**。两项跳过为需要 Docker Compose CLI 的环境配置测试，本机补跑 **2 passed**。JS 语法及 diff 检查通过。

同一固定发布镜像另启隔离服务，tmpfs 数据目录，无生产挂载、不开放公网端口。第一次启动因验收配置文件不存在而被入口守卫拒绝；提供独立空配置文件后正常启动。实际完成 admin 登录、独立测试项目保存、自定义字幕预览 HTTP 200，衬线字体、字号 60、正文 #12AB34、强调 #EF2468、手动两行均正确。healthz=ok、worker_alive=true。隔离容器在验收完成后停止并删除。

## 发布与线上验证

通过该提交的 `source/deploy/linux_ops.py` 执行 build → check-switch → publish。切换前 active_tasks 和 phase_blockers 均为空，allowed=true。管理器切换返回 status=ok。

切换后再次验证：production、目标 release、healthz=ok、worker_alive=true、maintenance=false；登录、工作台、三个脚本及项目列表 HTTP 200；app.js 包含统一字幕设置与保存预览实现。公网 `/home/healthz` 返回同一版本且健康。

未在生产创建测试制作/上传任务，外部 OSS/飞书/FBBM 写入链路未在此次发布复测。发布管理器保留失败自动恢复机制；本次成功未触发回滚。服务器发布目录保留 build.log、tests.log、release.json 和切换 history。
